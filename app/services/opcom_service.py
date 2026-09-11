"""Adaptor OPCOM PZU (Piata pentru Ziua Urmatoare).

Construieste dinamic URL-ul pentru ziua de livrare ceruta, pe baza formatului
exemplificat in cerinte:
  https://www.opcom.ro/rapoarte-pzu-raportPIP-export-csv/{dd}/{mm}/{yyyy}/ro?resolution=15

Schema CSV-ului (delimitator virgula, campuri incadrate in ghilimele,
coloana de pret "Pret de Inchidere a Pietei [lei/MWh]") e verificata
impotriva unui export real -- vezi `app/services/opcom_schema.py` si
`tests/unit/test_opcom_parser.py::test_parses_real_opcom_export_sample`.
Ramane neverificat doar fetch-ul HTTP live catre sursa (vezi
docs/LIMITATIONS.md sectiunea 1). Parserul valideaza EXPLICIT structura
gasita si NU presupune ca se potriveste implicit -- daca sursa e
inaccesibila sau schema nu se potriveste dupa toate reincercarile, se
foloseste (optional doar in dezvoltare/test, implicit dezactivat) un
fallback cu date sintetice, marcate clar ca atare in ImportRun si in UI.

Rezolutia intervalelor NU e fixa la 15 minute: parametrul `resolution=15`
din URL e doar o preferinta ceruta, dar OPCOM publica anii istorici la
rezolutie ORARA (PT60M, 24 intervale/zi) si alterneaza intre PT30M/PT15M
pentru anul curent, indiferent de parametrul cerut in URL -- `parse_csv`
citeste rezolutia REALA din coloana "Rezolutie" a CSV-ului (cand exista) si
valideaza numarul de intervale fata de aceasta, nu fata de o valoare fixa.
"""
from __future__ import annotations

import csv as csv_module
import hashlib
import io
from datetime import UTC, date, datetime, timedelta
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


DEFAULT_RESOLUTION_MINUTES = 15
# Anii istorici sunt publicati la rezolutie orara (PT60M, uneori scris PT1H);
# anul curent alterneaza intre PT30M si PT15M. Orice alta valoare e respinsa
# explicit, in loc sa fie interpretata tacit gresit.
_ISO8601_DURATION_MINUTES = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT1H": 60}


def _resolution_minutes(raw: str) -> int:
    key = raw.strip().upper()
    minutes = _ISO8601_DURATION_MINUTES.get(key)
    if minutes is None:
        raise OpcomParseError(
            f"Rezolutie OPCOM neasteptata/nesuportata: '{raw}' "
            f"(acceptate: {', '.join(sorted(_ISO8601_DURATION_MINUTES))})."
        )
    return minutes


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


def _split_rows(raw_text: str, delimiter: str) -> list[list[str]]:
    """Parseaza CSV-ul cu suport corect pentru campuri incadrate in ghilimele
    (RFC4180) -- OPCOM incadreaza fiecare camp in ghilimele duble, deci o
    simpla `line.split(delimiter)` ar rupe orice camp care contine el insusi
    delimitatorul (ex. un pret cu virgula zecimala intr-un CSV separat prin
    virgula). Rândurile complet goale sunt eliminate."""
    reader = csv_module.reader(io.StringIO(raw_text), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader]
    return [row for row in rows if any(cell for cell in row)]


def _find_header(rows: list[list[str]], schema: OpcomCsvSchema) -> tuple[int, dict[str, int]]:
    for idx, row in enumerate(rows[: schema.header_search_rows]):
        if len(row) < 2:
            # Un delimitator gresit (ex. cel implicit intr-un fisier care de
            # fapt foloseste alt separator) face ca intreaga linie sa devina
            # o SINGURA celula -- fara acest prag, potrivirea pe subsir de mai
            # jos ar putea gasi din greseala atat "interval" cat si "pret" in
            # aceeasi celula concatenata si ar accepta un header fals, in loc
            # sa lase controlul sa treaca la fallback-ul cu sniffer.
            continue
        cells = [c.lower() for c in row]
        # Potrivire pe SUBSIR, nu exacta: coloana reala de pret se numeste
        # "Pret de Inchidere a Pietei [lei/MWh]", nu doar "Pret" -- un tabel
        # sumar de mai sus in fisier (medii Base/Peak/Off-Peak) are propriile
        # coloane si e ignorat automat pentru ca nu are o coloana de interval.
        interval_idx = next(
            (i for i, c in enumerate(cells) if any(alias in c for alias in schema.interval_aliases)), None
        )
        price_idx = next(
            (i for i, c in enumerate(cells) if any(alias in c for alias in schema.price_aliases)), None
        )
        if interval_idx is not None and price_idx is not None and interval_idx != price_idx:
            currency_idx = next(
                (i for i, c in enumerate(cells) if any(alias in c for alias in schema.currency_aliases)), None
            )
            # Coloana de rezolutie (PT15M/PT30M/PT60M) e OPTIONALA -- CSV-urile simple
            # folosite de teste/fixture-uri sintetice nu o au, si parserul trebuie sa
            # continue sa functioneze neschimbat in acel caz (fallback la 15 minute).
            resolution_idx = next(
                (i for i, c in enumerate(cells) if any(alias in c for alias in schema.resolution_aliases)), None
            )
            mapping = {"interval": interval_idx, "price": price_idx}
            if currency_idx is not None:
                mapping["currency"] = currency_idx
            if resolution_idx is not None:
                mapping["resolution"] = resolution_idx
            return idx, mapping
    raise OpcomParseError(
        "Nu am putut identifica antetul CSV OPCOM cu schema configurata "
        f"(coloane cautate: {schema.interval_aliases} / {schema.price_aliases}). "
        f"Primele linii primite:\n{chr(10).join(schema.delimiter.join(r) for r in rows[:5])}"
    )


def parse_csv(raw_text: str, delivery_date: date, schema: OpcomCsvSchema = DEFAULT_SCHEMA) -> list[dict]:
    rows = _split_rows(raw_text, schema.delimiter)
    if not rows:
        raise OpcomParseError("Raspuns OPCOM gol.")

    try:
        header_idx, mapping = _find_header(rows, schema)
    except OpcomParseError as original_error:
        # Delimitatorul configurat nu a produs un header recunoscut -- incearca
        # sa detectam automat delimitatorul real din textul brut (fallback,
        # nu implicit), pentru cazul in care formatul sursei se schimba. Orice
        # esec al acestui fallback pastreaza eroarea ORIGINALA (nu una despre
        # sniffer), ca apelantii care prind explicit `OpcomParseError` (ex.
        # fallback-ul cu date sintetice) sa continue sa functioneze.
        try:
            detected = csv_module.Sniffer().sniff(raw_text[:5000], delimiters=";,\t|")
        except csv_module.Error:
            raise original_error from None
        if detected.delimiter == schema.delimiter:
            raise original_error from None
        rows = _split_rows(raw_text, detected.delimiter)
        try:
            header_idx, mapping = _find_header(rows, schema)
        except OpcomParseError:
            raise original_error from None

    data_rows = rows[header_idx + 1 :]

    parsed: dict[int, dict] = {}
    resolutions_seen: set[int] = set()
    for cells in data_rows:
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

        if "resolution" in mapping:
            resolutions_seen.add(_resolution_minutes(cells[mapping["resolution"]]))

        if interval_index in parsed:
            raise OpcomParseError(f"Interval duplicat in CSV OPCOM: {interval_index}.")
        parsed[interval_index] = {"price_mwh": price_mwh, "currency": currency}

    if not parsed:
        raise OpcomParseError("Nicio linie de date valida gasita in CSV-ul OPCOM.")

    if len(resolutions_seen) > 1:
        raise OpcomParseError(
            f"CSV OPCOM contine rezolutii diferite pentru aceeasi zi de livrare: "
            f"{sorted(resolutions_seen)} minute -- asteptata o singura rezolutie uniforma."
        )
    # Coloana de rezolutie e absenta in CSV-urile simple (teste/fixture-uri sintetice):
    # pastreaza comportamentul de dinainte, implicit 15 minute.
    resolution_minutes = resolutions_seen.pop() if resolutions_seen else DEFAULT_RESOLUTION_MINUTES

    count = len(parsed)
    start_local = datetime.combine(delivery_date, datetime.min.time(), tzinfo=BUCHAREST)
    next_local = datetime.combine(delivery_date + timedelta(days=1), datetime.min.time(), tzinfo=BUCHAREST)
    start_utc = start_local.astimezone(UTC)
    expected_count = int((next_local.astimezone(UTC) - start_utc).total_seconds() / (resolution_minutes * 60))
    if count != expected_count:
        raise OpcomParseError(
            f"Numar neasteptat de intervale ({count}); asteptat {expected_count} "
            f"pentru data {delivery_date.isoformat()} la rezolutie de {resolution_minutes} minute."
        )
    expected_indices = set(range(1, count + 1))
    missing = expected_indices - set(parsed.keys())
    if missing:
        raise OpcomParseError(f"Intervale lipsa in CSV OPCOM: {sorted(missing)[:10]}...")

    results = []
    for i in range(1, count + 1):
        item = parsed[i]
        interval_start = start_utc + timedelta(minutes=resolution_minutes * (i - 1))
        interval_end = interval_start + timedelta(minutes=resolution_minutes)
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
        with attempt, httpx.Client(timeout=settings.opcom_request_timeout_seconds) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
    raise OpcomFetchError("Eroare necunoscuta la preluarea CSV-ului OPCOM.")  # pragma: no cover


def has_successful_real_import(db: Session, delivery_date: date, source: str = "opcom_pzu") -> bool:
    """True daca exista deja o revizie reusita si NE-sintetica pentru aceasta zi.
    Folosit pentru a face importurile (zilnice si de backfill istoric) idempotente
    fara sa reinterogam sursa externa inutil."""
    existing = db.scalar(
        select(ImportRun).where(
            ImportRun.source == source,
            ImportRun.delivery_date == delivery_date,
            ImportRun.status == ImportRunStatus.succeeded.value,
            ImportRun.is_synthetic_fixture.is_(False),
        ).order_by(ImportRun.revision.desc()).limit(1)
    )
    return existing is not None


def get_real_imported_dates(db: Session, start: date, end: date, source: str = "opcom_pzu") -> set[date]:
    """O singura interogare care returneaza toate zilele din [start, end] care au
    deja o revizie reusita si ne-sintetica -- folosit de scriptul de backfill ca
    sa sara eficient peste zilele deja importate, fara N interogari separate."""
    rows = db.execute(
        select(ImportRun.delivery_date).where(
            ImportRun.source == source,
            ImportRun.delivery_date >= start,
            ImportRun.delivery_date <= end,
            ImportRun.status == ImportRunStatus.succeeded.value,
            ImportRun.is_synthetic_fixture.is_(False),
        ).distinct()
    ).all()
    return {r[0] for r in rows}


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
