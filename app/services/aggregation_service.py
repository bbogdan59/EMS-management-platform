"""Agregare energetica idempotenta: telemetrie bruta (telemetry_raw) ->
agregate pe interval de 15 minute, ora, zi si luna (telemetry_aggregates).

Idempotenta: fiecare rulare foloseste UPSERT (ON CONFLICT ... DO UPDATE) pe
constrangerea unica (station_id, period_type, period_start), deci rularea
repetata pentru acelasi interval produce acelasi rezultat, nu duplicate --
inclusiv reluari ("replay") peste date deja agregate, dupa sosirea unor
esantioane intarziate.

Un interval cu ZERO esantioane brute nu produce niciun rand (nu unul cu
valori 0). Un interval CU esantioane, dar in care o metrica anume nu are
nicio acoperire (ex. dispozitivul nu a raportat niciodata `ev_power_w`),
produce un rand cu acea metrica NULL -- necunoscut nu inseamna zero.

## Contractul de integrare temporala (AC energie)

Fiecare metrica de putere instantanee (W) e integrata in energie (kWh)
folosind conventia "zero-order hold" (ZOH): valoarea unui esantion se
considera constanta de la momentul lui pana la urmatorul esantion cunoscut
al ACELEIASI metrici, dar niciodata mai mult de `MAX_GAP_SECONDS`. Dincolo
de acest prag, seria e considerata necunoscuta (nu se extrapoleaza) -- acea
portiune din interval NU contribuie la energie si scade `coverage`
(fractia din durata intervalului acoperita efectiv de date), raportat
separat pentru fiecare metrica: "pv", "load", "battery", "grid", "ev", "soc".
`battery` acopera atat incarcarea cat si descarcarea (ambele derivate din
acelasi camp brut `battery_power_w`); la fel `grid` pentru import/export.

## Contractul AC/DC pentru puterea bateriei

`TelemetryRaw.battery_power_w` e raportat de dispozitiv la bornele DC ale
bateriei (nu la iesirea AC a invertorului) -- e convenția API-ului de
dispozitive (`docs/API.md`), nu ceva calculat aici. Platforma NU aplica nicio
conversie AC/DC suplimentara peste ce trimite dispozitivul: energia de
incarcare/descarcare publicata (`battery_charge_energy_kwh`/
`battery_discharge_energy_kwh`) e energia DC masurata, fara randamentul de
conversie al invertorului aplicat (acela se aplica separat, explicit, in
motorul de optimizare -- `battery_charge_efficiency`/
`battery_discharge_efficiency` din `StationConfigVersion` -- niciodata
implicit aici). `battery_reference_capacity_kwh` (nameplate) si
`battery_available_capacity_kwh` (utilizabila, poate scadea din degradare)
raman separate in configuratia statiei; EFC-ul (`dashboard_service.
get_efc_used`) foloseste explicit capacitatea de REFERINTA ca numitor, nu
cea disponibila -- un ciclu complet inseamna "cat la suta din bateria
nou-instalata", nu din cea curenta (altfel EFC-ul ar "creste" artificial pe
masura ce bateria se degradeaza, desi utilizarea reala e aceeasi).

## Calitatea datelor (`data_quality`)

Derivata, cea mai "slaba" castiga (simulated > estimated > measured):
'simulated' daca orice proba provine din simulator, 'estimated' daca
numarul brut de esantioane e sub pragul minim SAU acoperirea vreunei
metrici e sub `FULL_COVERAGE_QUALITY_THRESHOLD`, altfel 'measured'.

## Calendar local al statiei (zi/luna)

`aggregate_day`/`aggregate_month` primesc statia (nu doar id-ul) pentru ca
au nevoie de `station.timezone` -- limitele "zi"/"luna" sunt calculate in
ora locala a statiei, convertite in UTC exact ca in `opcom_service`
(delta reala intre miezurile de noapte locale, nu +24h fix), deci zilele cu
schimbare de ora (23h primavara, 25h toamna) sunt agregate corect, fara sa
piarda sau sa dubleze o ora. Presupunere documentata: offset-ul UTC al
fusului orar al statiei e un numar intreg de ore (adevarat pentru
Europe/Bucharest, singurul fus folosit curent) -- altfel limitele de ora
UTC (`aggregate_hour`) nu s-ar mai alinia exact cu miezul noptii locale.

## Backfill / date intarziate

`reaggregate_range` reface, idempotent, toate nivelurile (15m -> ora -> zi
-> luna) atinse de un interval arbitrar -- e folosita atat de
`run_aggregation_task` (fereastra scurta, recenta, la fiecare rulare, ca sa
prinda automat esantioanele usor intarziate) cat si pentru un backfill
manual pe un interval istoric arbitrar (dupa o intrerupere lunga a unui
dispozitiv, de exemplu).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.station import Station
from app.models.telemetry import TelemetryAggregate, TelemetryRaw

QUALITY_RANK = {"measured": 0, "estimated": 1, "simulated": 2, "stale": 3, "missing": 4}
MIN_SAMPLES_FOR_MEASURED_15M = 2

# O metrica e considerata cunoscuta intre doua esantioane consecutive doar
# daca golul dintre ele nu depaseste acest prag; peste el, portiunea ramasa
# e "necunoscuta", nu extrapolata. Ales ca multiplu generos al intervalului
# tipic de polling al simulatorului/dispozitivelor (implicit 20s) --
# suficient pentru jitter/retry normal, dar exclude opriri reale.
MAX_GAP_SECONDS = 300

# Sub acest prag de acoperire pentru orice metrica, randul e retrogradat cel
# putin la "estimated" -- coverage mic inseamna extrapolare semnificativa.
FULL_COVERAGE_QUALITY_THRESHOLD = 0.9

# Fereastra de recalculare folosita de task-ul periodic (vezi
# app/workers/tasks.py::run_aggregation_task) ca sa prinda automat
# esantioanele intarziate recent, fara sa astepte un backfill manual.
RECENT_REAGGREGATION_LOOKBACK = timedelta(hours=3)

_METRIC_KEYS = ("pv", "load", "battery", "grid", "ev", "soc")


def _worst_quality(qualities: list[str]) -> str:
    return max(qualities, key=lambda q: QUALITY_RANK.get(q, 0)) if qualities else "missing"


def _segments(
    rows: list[TelemetryRaw],
    getter,
    period_start: datetime,
    period_end: datetime,
    max_gap: timedelta,
) -> list[tuple[datetime, datetime, float]]:
    """Descompune o metrica in segmente [seg_start, seg_end, value) prin
    "zero-order hold" intre esantioane consecutive cunoscute, taiate la
    limitele intervalului si la `max_gap`. Vezi contractul din docstring-ul
    modulului."""
    samples = sorted(
        (r.measured_at, float(v)) for r in rows if (v := getter(r)) is not None
    )
    if not samples:
        return []

    segments = []
    for i, (ts, value) in enumerate(samples):
        reach_end = ts + max_gap
        if i + 1 < len(samples):
            next_ts = samples[i + 1][0]
            if next_ts < reach_end:
                reach_end = next_ts
        seg_start = max(ts, period_start)
        seg_end = min(reach_end, period_end)
        if seg_end > seg_start:
            segments.append((seg_start, seg_end, value))
    return segments


def _integrate(
    rows: list[TelemetryRaw], getter, period_start: datetime, period_end: datetime, max_gap: timedelta
) -> tuple[Decimal | None, float]:
    """Energie (kWh) integrata in timp peste o metrica scalara >= 0 (pv/load/ev)."""
    segments = _segments(rows, getter, period_start, period_end, max_gap)
    if not segments:
        return None, 0.0
    total_seconds = (period_end - period_start).total_seconds()
    covered = 0.0
    energy_ws = 0.0
    for seg_start, seg_end, value in segments:
        dur = (seg_end - seg_start).total_seconds()
        covered += dur
        energy_ws += value * dur
    coverage = min(covered / total_seconds, 1.0) if total_seconds > 0 else 0.0
    energy_kwh = energy_ws / 3_600_000.0
    return Decimal(str(round(energy_kwh, 6))), round(coverage, 4)


def _integrate_signed_split(
    rows: list[TelemetryRaw], getter, period_start: datetime, period_end: datetime, max_gap: timedelta
) -> tuple[Decimal | None, Decimal | None, float]:
    """Ca `_integrate`, dar pentru o metrica cu semn (battery/grid): imparte
    integrala in partea pozitiva si valoarea absoluta a partii negative,
    pastrand o singura acoperire comuna (aceeasi serie bruta)."""
    segments = _segments(rows, getter, period_start, period_end, max_gap)
    if not segments:
        return None, None, 0.0
    total_seconds = (period_end - period_start).total_seconds()
    covered = 0.0
    pos_ws = 0.0
    neg_ws = 0.0
    for seg_start, seg_end, value in segments:
        dur = (seg_end - seg_start).total_seconds()
        covered += dur
        if value >= 0:
            pos_ws += value * dur
        else:
            neg_ws += -value * dur
    coverage = min(covered / total_seconds, 1.0) if total_seconds > 0 else 0.0
    pos_kwh = Decimal(str(round(pos_ws / 3_600_000.0, 6)))
    neg_kwh = Decimal(str(round(neg_ws / 3_600_000.0, 6)))
    return pos_kwh, neg_kwh, round(coverage, 4)


def _time_weighted_average(
    rows: list[TelemetryRaw], getter, period_start: datetime, period_end: datetime, max_gap: timedelta
) -> tuple[Decimal | None, float]:
    """Media ponderata in timp a unei marimi de stare (SOC), NU o energie --
    aceeasi descompunere ZOH, dar impartita la durata acoperita, nu la 3600."""
    segments = _segments(rows, getter, period_start, period_end, max_gap)
    if not segments:
        return None, 0.0
    total_seconds = (period_end - period_start).total_seconds()
    covered = 0.0
    weighted_sum = 0.0
    for seg_start, seg_end, value in segments:
        dur = (seg_end - seg_start).total_seconds()
        covered += dur
        weighted_sum += value * dur
    if covered <= 0:
        return None, 0.0
    coverage = min(covered / total_seconds, 1.0) if total_seconds > 0 else 0.0
    return Decimal(str(round(weighted_sum / covered, 2))), round(coverage, 4)


def aggregate_interval_15m(db: Session, station_id: uuid.UUID, period_start: datetime) -> dict | None:
    period_end = period_start + timedelta(minutes=15)
    max_gap = timedelta(seconds=MAX_GAP_SECONDS)

    in_interval = db.scalars(
        select(TelemetryRaw).where(
            TelemetryRaw.station_id == station_id,
            TelemetryRaw.measured_at >= period_start,
            TelemetryRaw.measured_at < period_end,
        )
    ).all()
    if not in_interval:
        return None  # fara nicio proba in interval -> fara rand, nu zero fabricat

    # Context suplimentar pentru "carry-in": ultimele esantioane dinaintea
    # intervalului, in limita MAX_GAP_SECONDS, ca sa poata acoperi inceputul
    # intervalului chiar daca primul esantion propriu-zis vine putin mai tarziu.
    carry_in = db.scalars(
        select(TelemetryRaw).where(
            TelemetryRaw.station_id == station_id,
            TelemetryRaw.measured_at >= period_start - max_gap,
            TelemetryRaw.measured_at < period_start,
        )
    ).all()
    rows = [*carry_in, *in_interval]
    n = len(in_interval)

    pv_kwh, pv_cov = _integrate(rows, lambda r: r.pv_power_w, period_start, period_end, max_gap)
    load_kwh, load_cov = _integrate(rows, lambda r: r.load_power_w, period_start, period_end, max_gap)
    batt_charge_kwh, batt_discharge_kwh, batt_cov = _integrate_signed_split(
        rows, lambda r: r.battery_power_w, period_start, period_end, max_gap
    )
    grid_import_kwh, grid_export_kwh, grid_cov = _integrate_signed_split(
        rows, lambda r: r.grid_power_w, period_start, period_end, max_gap
    )
    ev_kwh, ev_cov = _integrate(rows, lambda r: r.ev_power_w, period_start, period_end, max_gap)
    avg_soc, soc_cov = _time_weighted_average(rows, lambda r: r.battery_soc_percent, period_start, period_end, max_gap)

    coverage = {"pv": pv_cov, "load": load_cov, "battery": batt_cov, "grid": grid_cov, "ev": ev_cov, "soc": soc_cov}

    if any(r.is_simulated for r in in_interval):
        quality = "simulated"
    elif n < MIN_SAMPLES_FOR_MEASURED_15M or any(c < FULL_COVERAGE_QUALITY_THRESHOLD for c in coverage.values() if c > 0):
        quality = "estimated"
    else:
        quality = "measured"

    values = {
        "id": uuid.uuid4(),
        "station_id": station_id,
        "period_type": "interval_15m",
        "period_start": period_start,
        "period_end": period_end,
        "pv_energy_kwh": pv_kwh,
        "load_energy_kwh": load_kwh,
        "battery_charge_energy_kwh": batt_charge_kwh,
        "battery_discharge_energy_kwh": batt_discharge_kwh,
        "grid_import_energy_kwh": grid_import_kwh,
        "grid_export_energy_kwh": grid_export_kwh,
        "ev_energy_kwh": ev_kwh,
        "avg_battery_soc_percent": avg_soc,
        "sample_count": n,
        "data_quality": quality,
        "coverage": coverage,
    }
    _upsert_aggregate(db, values)
    return values


def _rollup(
    db: Session, station_id: uuid.UUID, source_period_type: str, target_period_type: str, period_start: datetime, period_end: datetime
) -> dict | None:
    rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station_id,
            TelemetryAggregate.period_type == source_period_type,
            TelemetryAggregate.period_start >= period_start,
            TelemetryAggregate.period_start < period_end,
        )
    ).all()
    if not rows:
        return None

    total_seconds = (period_end - period_start).total_seconds()

    def total_or_none(attr: str) -> Decimal | None:
        values = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        if not values:
            return None
        return Decimal(str(round(sum(float(v) for v in values), 6)))

    def rolled_coverage(key: str) -> float:
        covered_seconds = sum(
            (r.period_end - r.period_start).total_seconds() * (r.coverage or {}).get(key, 0.0) for r in rows
        )
        return round(min(covered_seconds / total_seconds, 1.0), 4) if total_seconds > 0 else 0.0

    coverage = {key: rolled_coverage(key) for key in _METRIC_KEYS}

    soc_weighted = sum(
        float(r.avg_battery_soc_percent) * (r.period_end - r.period_start).total_seconds() * (r.coverage or {}).get("soc", 0.0)
        for r in rows
        if r.avg_battery_soc_percent is not None
    )
    soc_covered_seconds = sum((r.period_end - r.period_start).total_seconds() * (r.coverage or {}).get("soc", 0.0) for r in rows)
    avg_soc = Decimal(str(round(soc_weighted / soc_covered_seconds, 2))) if soc_covered_seconds > 0 else None

    total_samples = sum(r.sample_count for r in rows)

    values = {
        "id": uuid.uuid4(),
        "station_id": station_id,
        "period_type": target_period_type,
        "period_start": period_start,
        "period_end": period_end,
        "pv_energy_kwh": total_or_none("pv_energy_kwh"),
        "load_energy_kwh": total_or_none("load_energy_kwh"),
        "battery_charge_energy_kwh": total_or_none("battery_charge_energy_kwh"),
        "battery_discharge_energy_kwh": total_or_none("battery_discharge_energy_kwh"),
        "grid_import_energy_kwh": total_or_none("grid_import_energy_kwh"),
        "grid_export_energy_kwh": total_or_none("grid_export_energy_kwh"),
        "ev_energy_kwh": total_or_none("ev_energy_kwh"),
        "avg_battery_soc_percent": avg_soc,
        "sample_count": total_samples,
        "data_quality": _worst_quality([r.data_quality for r in rows]),
        "coverage": coverage,
    }
    _upsert_aggregate(db, values)
    return values


def aggregate_hour(db: Session, station_id: uuid.UUID, hour_start: datetime) -> dict | None:
    return _rollup(db, station_id, "interval_15m", "hour", hour_start, hour_start + timedelta(hours=1))


def _local_midnight_utc(tz: ZoneInfo, local_date: date) -> datetime:
    return datetime.combine(local_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)


def aggregate_day(db: Session, station: Station, local_date: date) -> dict | None:
    """Agrega o zi in calendarul LOCAL al statiei (`station.timezone`), nu in
    UTC -- limitele reale (23h/25h in zilele cu schimbare de ora) sunt
    calculate din delta UTC intre doua miezuri de noapte locale consecutive,
    la fel ca in `opcom_service.parse_csv`."""
    tz = ZoneInfo(station.timezone)
    day_start_utc = _local_midnight_utc(tz, local_date)
    day_end_utc = _local_midnight_utc(tz, local_date + timedelta(days=1))
    return _rollup(db, station.id, "hour", "day", day_start_utc, day_end_utc)


def aggregate_month(db: Session, station: Station, year: int, month: int) -> dict | None:
    tz = ZoneInfo(station.timezone)
    month_start_local = date(year, month, 1)
    next_month_local = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    month_start_utc = _local_midnight_utc(tz, month_start_local)
    month_end_utc = _local_midnight_utc(tz, next_month_local)
    return _rollup(db, station.id, "day", "month", month_start_utc, month_end_utc)


def reaggregate_range(db: Session, station: Station, start_utc: datetime, end_utc: datetime) -> None:
    """Reface (idempotent) toate nivelurile de agregare atinse de
    [start_utc, end_utc): 15 minute -> ora -> zi (calendar local) -> luna.
    Sigur de rulat repetat pe acelasi interval (ON CONFLICT DO UPDATE) --
    folosita atat pentru recuperarea automata a datelor usor intarziate
    (`run_aggregation_task`), cat si pentru un backfill/replay manual pe un
    interval istoric arbitrar."""
    if end_utc <= start_utc:
        return
    tz = ZoneInfo(station.timezone)

    bucket_start = start_utc.replace(minute=(start_utc.minute // 15) * 15, second=0, microsecond=0)
    hours_touched: set[datetime] = set()
    t = bucket_start
    while t < end_utc:
        aggregate_interval_15m(db, station.id, t)
        hours_touched.add(t.replace(minute=0, second=0, microsecond=0))
        t += timedelta(minutes=15)

    days_touched: set[date] = set()
    for hour_start in sorted(hours_touched):
        aggregate_hour(db, station.id, hour_start)
        days_touched.add(hour_start.astimezone(tz).date())

    months_touched: set[tuple[int, int]] = set()
    for local_day in sorted(days_touched):
        aggregate_day(db, station, local_day)
        months_touched.add((local_day.year, local_day.month))

    for year, month in sorted(months_touched):
        aggregate_month(db, station, year, month)


def get_energy_kwh_for_interval(db: Session, station_id: uuid.UUID, metric: str, start_utc: datetime, end_utc: datetime) -> dict:
    """Contract de citire pentru consumatori externi (ex. optimizatorul, care
    NU e modificat aici sa-l foloseasca inca): energia (kWh) pentru o
    metrica data, pe un interval arbitrar, plus acoperirea reala.

    `metric` e una din "pv", "load", "battery_charge", "battery_discharge",
    "grid_import", "grid_export", "ev" (corespund direct coloanelor
    `TelemetryAggregate`). Insumeaza doar agregatele `interval_15m` complet
    continute in [start_utc, end_utc) -- un capat care nu cade exact pe o
    granita de 15 minute nu e portionat, ci exclus (contract explicit, nu o
    aproximare silentioasa). Randul intors:
      {"energy_kwh": Decimal | None, "coverage": float, "buckets_found": int,
       "buckets_expected": int}
    `energy_kwh` e None doar daca NICIUN bucket din interval are date pentru
    acea metrica; altfel e suma partiala a ce exista, iar `coverage` (0..1,
    ponderata pe durata reala acoperita, nu doar pe numarul de bucket-uri)
    ii spune apelantului cat de completa e suma -- decizia asupra pragului
    acceptabil ramane a apelantului, nu se presupune aici."""
    column = f"{metric}_energy_kwh"
    coverage_key = "battery" if metric.startswith("battery_") else ("grid" if metric.startswith("grid_") else metric)

    aligned_start = start_utc.replace(minute=(start_utc.minute // 15) * 15, second=0, microsecond=0)
    buckets_expected = max(int((end_utc - aligned_start).total_seconds() // 900), 0)

    rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station_id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start >= start_utc,
            TelemetryAggregate.period_end <= end_utc,
        )
    ).all()

    total_seconds = (end_utc - start_utc).total_seconds()
    covered_seconds = 0.0
    energy_total: Decimal | None = None
    for r in rows:
        value = getattr(r, column)
        cov = (r.coverage or {}).get(coverage_key, 0.0)
        covered_seconds += (r.period_end - r.period_start).total_seconds() * cov
        if value is not None:
            energy_total = value if energy_total is None else energy_total + value

    coverage = round(min(covered_seconds / total_seconds, 1.0), 4) if total_seconds > 0 else 0.0
    return {
        "energy_kwh": energy_total,
        "coverage": coverage,
        "buckets_found": len(rows),
        "buckets_expected": buckets_expected,
    }


def _upsert_aggregate(db: Session, values: dict) -> None:
    stmt = pg_insert(TelemetryAggregate).values(**values)
    update_cols = {
        k: stmt.excluded[k]
        for k in values
        if k not in ("id", "station_id", "period_type", "period_start")
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["station_id", "period_type", "period_start"], set_=update_cols
    )
    db.execute(stmt)
    # Un UPDATE facut prin Core (ca mai sus) nu invalideaza automat un obiect
    # ORM deja incarcat pentru acelasi rand in identity map-ul sesiunii --
    # fara asta, un apel ulterior de reagregare (ex. backfill dupa date
    # intarziate) ar lasa consumatorii sa vada in continuare valorile vechi,
    # in cache, desi randul din baza s-a schimbat deja.
    db.expire_all()
