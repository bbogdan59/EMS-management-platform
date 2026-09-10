"""Adaptor OPCOM PZU (Piata pentru Ziua Urmatoare).

Construieste dinamic URL-ul pentru ziua de livrare ceruta, pe baza formatului
exemplificat in cerinte:
  https://www.opcom.ro/rapoarte-pzu-raportPIP-export-csv/{dd}/{mm}/{yyyy}/ro?resolution=15

LIMITARE DOCUMENTATA: schema exacta a CSV-ului (nume de coloane, separator,
encoding) nu a putut fi verificata direct impotriva sursei reale in mediul in
care a fost dezvoltata platforma (acces retea blocat de politica organizatiei
gazda). Parserul valideaza EXPLICIT structura gasita si NU presupune ca se
potriveste implicit -- daca sursa e inaccesibila sau schema nu se potriveste
dupa toate reincercarile, se foloseste (optional doar in dezvoltare/test, implicit dezactivat) un
fallback cu date sintetice, marcate clar ca atare in ImportRun si in UI.
"""
from __future__ import annotations

import csv as csv_module
import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.enums import AlertSeverity, ImportRunStatus
from app.models.market import ImportRun, MarketPriceInterval
from app.services.opcom_fixtures import generate_synthetic_csv
from app.services.opcom_schema import DEFAULT_SCHEMA, OpcomCsvSchema

logger = structlog.get_logger(__name__)
settings = get_settings()
BUCHAREST = ZoneInfo("Europe/Bucharest")


class OpcomError(Exception):
    pass


class OpcomFetchError(OpcomError):
    pass


class OpcomParseError(OpcomError):
    pass


def build_source_url(delivery_date: date) -> str:
    return f"{settings.opcom_base_url}/{delivery_date:%d}/{delivery_date:%m}/{delivery_date:%Y}/ro?resolution=15"


def _decode(raw: bytes, schema: OpcomCsvSchema) -> str:
    for enc in schema.encoding_candidates:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_price(value: str) -> Decimal:
    v = value.strip()
    if "," in v and "." in v:
        # The rightmost separator is decimal: accept both 1.234,56 and 1,234.56.
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")
        else:
            v = v.replace(",", "")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        price = Decimal(v)
        if not price.is_finite():
            raise InvalidOperation
        return price
    except InvalidOperation as exc:
        raise OpcomParseError(f"Valoare de pret nenumerica: '{value}'") from exc


def _find_header(lines: list[str], schema: OpcomCsvSchema) -> tuple[int, dict[str, int], str]:
    delimiter = schema.delimiter
    for idx, line in enumerate(lines[: schema.header_search_rows]):
        if not line.strip():
            continue
        cells = [c.strip().lower() for c in line.split(delimiter)]
        interval_idx = next((i for i, c in enumerate(cells) if c in schema.interval_aliases), None)
        price_idx = next((i for i, c in enumerate(cells) if c in schema.price_aliases), None)
        if interval_idx is not None and price_idx is not None:
            currency_idx = next((i for i, c in enumerate(cells) if c in schema.currency_aliases), None)
            mapping = {"interval": interval_idx, "price": price_idx}
            if currency_idx is not None:
                mapping["currency"] = currency_idx
            return idx, mapping, delimiter

    # Sniffer ca fallback daca delimitatorul configurat nu produce un header recunoscut.
    sample = "\n".join(lines[: schema.header_search_rows])
    try:
        detected = csv_module.Sniffer().sniff(sample, delimiters=";,\t|")
        if detected.delimiter != delimiter:
            return _find_header(lines, OpcomCsvSchema(**{**schema.__dict__, "delimiter": detected.delimiter}))
    except csv_module.Error:
        pass

    preview = "\n".join(lines[:5])
    raise OpcomParseError(
        "Nu am putut identifica antetul CSV OPCOM cu schema configurata "
        f"(coloane cautate: {schema.interval_aliases} / {schema.price_aliases}). "
        f"Primele linii primite:\n{preview}"
    )


def parse_csv(raw_text: str, delivery_date: date, schema: OpcomCsvSchema = DEFAULT_SCHEMA) -> list[dict]:
    lines = [ln for ln in raw_text.splitlines() if ln.strip() != ""]
    if not lines:
        raise OpcomParseError("Raspuns OPCOM gol.")

    header_idx, mapping, delimiter = _find_header(lines, schema)
    data_lines = lines[header_idx + 1 :]

    parsed: dict[int, dict] = {}
    for line in data_lines:
        cells = [c.strip() for c in line.split(delimiter)]
        if len(cells) <= max(mapping.values()):
            continue
        try:
            interval_index = int(cells[mapping["interval"]])
        except ValueError:
            continue

        price_mwh = _parse_price(cells[mapping["price"]])

        currency = "RON"
        if "currency" in mapping:
            currency_raw = cells[mapping["currency"]].strip().upper()
            if currency_raw not in ("RON", "LEI"):
                raise OpcomParseError(f"Moneda neasteptata in CSV OPCOM: '{currency_raw}' (asteptat RON).")
            currency = "RON"

        if interval_index in parsed:
            raise OpcomParseError(f"Interval duplicat in CSV OPCOM: {interval_index}.")
        parsed[interval_index] = {"price_mwh": price_mwh, "currency": currency}

    if not parsed:
        raise OpcomParseError("Nicio linie de date valida gasita in CSV-ul OPCOM.")

    count = len(parsed)
    start_local = datetime.combine(delivery_date, datetime.min.time(), tzinfo=BUCHAREST)
    next_local = datetime.combine(delivery_date + timedelta(days=1), datetime.min.time(), tzinfo=BUCHAREST)
    start_utc = start_local.astimezone(timezone.utc)
    expected_count = int((next_local.astimezone(timezone.utc) - start_utc).total_seconds() / 900)
    if count != expected_count:
        raise OpcomParseError(
            f"Numar neasteptat de intervale ({count}); asteptat {expected_count} "
            f"pentru data {delivery_date.isoformat()} la rezolutie de 15 minute."
        )
    expected_indices = set(range(1, count + 1))
    missing = expected_indices - set(parsed.keys())
    if missing:
        raise OpcomParseError(f"Intervale lipsa in CSV OPCOM: {sorted(missing)[:10]}...")

    results = []
    for i in range(1, count + 1):
        item = parsed[i]
        interval_start = start_utc + timedelta(minutes=15 * (i - 1))
        interval_end = interval_start + timedelta(minutes=15)
        price_kwh = (item["price_mwh"] / Decimal(1000)).quantize(Decimal("0.000001"))
        results.append(
            {
                "interval_index": i,
                "interval_start": interval_start,
                "interval_end": interval_end,
                "currency": item["currency"],
                "price_lei_per_mwh": item["price_mwh"],
                "price_lei_per_kwh": price_kwh,
                "is_negative": item["price_mwh"] < 0,
            }
        )
    return results


def _fetch_raw(url: str) -> bytes:
    for attempt in Retrying(
        stop=stop_after_attempt(settings.opcom_max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type((httpx.HTTPError,)),
        reraise=True,
    ):
        with attempt:
            with httpx.Client(timeout=settings.opcom_request_timeout_seconds) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
    raise OpcomFetchError("Eroare necunoscuta la preluarea CSV-ului OPCOM.")  # pragma: no cover


def import_opcom_day(db: Session, delivery_date: date, triggered_by_user_id=None) -> ImportRun:
    source = "opcom_pzu"
    existing_max = db.scalar(
        select(ImportRun.revision)
        .where(ImportRun.source == source, ImportRun.delivery_date == delivery_date)
        .order_by(ImportRun.revision.desc())
        .limit(1)
    )
    revision = (existing_max or 0) + 1
    url = build_source_url(delivery_date)

    run = ImportRun(
        source=source,
        delivery_date=delivery_date,
        revision=revision,
        status=ImportRunStatus.running.value,
        source_url=url,
        triggered_by_user_id=triggered_by_user_id,
    )
    db.add(run)
    db.flush()

    today_local = datetime.now(BUCHAREST).date()
    if delivery_date > today_local + timedelta(days=1):
        run.status = ImportRunStatus.unpublished.value
        run.error_message = (
            "Data de livrare este prea indepartata in viitor. Preturile PZU se publica de "
            "regula cu o zi inainte de ziua de livrare -- nu exista inca date de importat."
        )
        db.add(run)
        db.flush()
        return run

    raw_bytes: bytes | None = None
    fetch_error: Exception | None = None
    try:
        raw_bytes = _fetch_raw(url)
        run.attempt_count = settings.opcom_max_retries
    except Exception as exc:  # httpx.HTTPError sau eroare finala dupa retry
        fetch_error = exc
        run.attempt_count = settings.opcom_max_retries

    run.fetched_at = utcnow()

    intervals: list[dict] | None = None
    is_synthetic = False

    if raw_bytes is not None:
        raw_text = _decode(raw_bytes, DEFAULT_SCHEMA)
        run.raw_response = raw_text[:200_000]
        run.raw_response_hash = hashlib.sha256(raw_bytes).hexdigest()
        try:
            intervals = parse_csv(raw_text, delivery_date)
        except OpcomParseError as exc:
            fetch_error = exc

    if intervals is None and settings.opcom_use_synthetic_fixture_on_failure:
        synthetic_csv = generate_synthetic_csv(delivery_date)
        run.raw_response = synthetic_csv
        run.raw_response_hash = hashlib.sha256(synthetic_csv.encode("utf-8")).hexdigest()
        is_synthetic = True
        try:
            intervals = parse_csv(synthetic_csv, delivery_date)
        except OpcomParseError:
            intervals = None
        run.error_message = (
            f"Sursa OPCOM indisponibila ({fetch_error}); s-au folosit date SINTETICE de fallback, "
            "marcate ca atare."
        )
        logger.warning("opcom.fallback_synthetic", delivery_date=str(delivery_date), reason=str(fetch_error))

    if intervals is None:
        run.status = ImportRunStatus.failed.value
        run.error_message = run.error_message or str(fetch_error)
        db.add(
            Alert(
                station_id=None,
                category="opcom_import_failed",
                severity=AlertSeverity.warning.value,
                status="open",
                title=f"Import OPCOM esuat pentru {delivery_date.isoformat()}",
                description=run.error_message,
                context={"import_run_id": str(run.id)},
            )
        )
        db.flush()
        return run

    db.execute(
        update(MarketPriceInterval)
        .where(MarketPriceInterval.source == source, MarketPriceInterval.delivery_date == delivery_date)
        .values(is_current=False)
    )

    for item in intervals:
        db.add(
            MarketPriceInterval(
                import_run_id=run.id,
                source=source,
                delivery_date=delivery_date,
                revision=revision,
                interval_index=item["interval_index"],
                interval_start=item["interval_start"],
                interval_end=item["interval_end"],
                currency=item["currency"],
                price_lei_per_mwh=item["price_lei_per_mwh"],
                price_lei_per_kwh=item["price_lei_per_kwh"],
                is_negative=item["is_negative"],
                is_current=True,
            )
        )

    run.is_synthetic_fixture = is_synthetic
    run.interval_count = len(intervals)
    run.status = ImportRunStatus.succeeded.value
    db.add(run)
    db.flush()
    return run
