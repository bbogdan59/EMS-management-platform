from __future__ import annotations

import bisect
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.security import utcnow
from app.models.device import Device
from app.models.enums import PlanStatus
from app.models.market import MarketPriceInterval
from app.models.optimization import Plan, PlanInterval
from app.models.station import Station, StationConfigVersion
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services import chart_aggregation, tariff_service

STALE_AFTER = timedelta(minutes=10)
ENERGY_KPI_COVERAGE_PARTIAL_BELOW = 0.9


def _station_tz(station: Station) -> ZoneInfo:
    try:
        return ZoneInfo(station.timezone)
    except Exception:
        return ZoneInfo("Europe/Bucharest")


def get_latest_telemetry(db: Session, station_id: uuid.UUID) -> TelemetryRaw | None:
    return db.scalar(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station_id)
        .order_by(TelemetryRaw.measured_at.desc())
        .limit(1)
    )


def get_summary(db: Session, station: Station) -> dict:
    latest = get_latest_telemetry(db, station.id)
    now = utcnow()

    data_quality = "missing"
    last_update = None
    if latest is not None:
        last_update = latest.measured_at
        if latest.is_simulated:
            data_quality = "simulated"
        elif (now - latest.measured_at) > STALE_AFTER:
            data_quality = "stale"
        else:
            data_quality = "measured"

    import_tariff = tariff_service.get_current_tariff_version(db, station.id, "import", now)
    export_tariff = tariff_service.get_current_tariff_version(db, station.id, "export", now)
    market_price = db.scalar(
        select(MarketPriceInterval)
        .where(
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start <= now,
            MarketPriceInterval.interval_end > now,
        )
        .order_by(MarketPriceInterval.interval_start.desc())
        .limit(1)
    )

    price_buy = _effective_price(import_tariff, market_price)
    price_sell = _effective_price(export_tariff, market_price)

    plan = db.scalar(
        select(Plan)
        .where(Plan.station_id == station.id, Plan.status.in_([PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value]))
        .order_by(Plan.version.desc())
        .limit(1)
    )

    device_online = db.scalar(
        select(Device.id).where(Device.station_id == station.id, Device.last_heartbeat_at.isnot(None), Device.last_heartbeat_at > now - timedelta(minutes=5))
    ) is not None

    return {
        "pv_power_kw": _w_to_kw(latest.pv_power_w) if latest else None,
        "load_power_kw": _w_to_kw(latest.load_power_w) if latest else None,
        "battery_power_kw": _w_to_kw(latest.battery_power_w) if latest else None,
        "grid_power_kw": _w_to_kw(latest.grid_power_w) if latest else None,
        "battery_soc_percent": float(latest.battery_soc_percent) if latest and latest.battery_soc_percent is not None else None,
        "ev_connected": latest.ev_connected if latest else None,
        "ev_power_kw": _w_to_kw(latest.ev_power_w) if latest else None,
        "price_buy_lei_kwh": price_buy,
        "price_sell_lei_kwh": price_sell,
        "execution_mode": station.execution_mode,
        "has_active_plan": plan is not None,
        "plan_status": plan.status if plan else None,
        "device_online": device_online,
        "last_update": last_update.isoformat() if last_update else None,
        "data_quality": data_quality,
        "timezone": station.timezone,
        # Provenienta ultimei telemetrii (issue #43) -- `device_rs485` sau
        # `deye_cloud`. Regula de prioritate (dispozitiv local activ ->
        # Deye Cloud nu mai scrie) e aplicata la INGERARE
        # (`deye_cloud_service.poll_connection`), nu aici -- randul "cel mai
        # recent" e deja cel corect de afisat, fara logica suplimentara.
        "telemetry_source": latest.source if latest else None,
    }


def get_live_metrics(db: Session, station: Station) -> list[dict]:
    """Contract per-metrica versionat pentru fluxul SSE (issue #50): fiecare
    intrare descrie explicit `metric`/`value`/`unit`/`measured_at`/
    `received_at`/`quality`/`source`, distinct de `get_summary` (folosit
    NESCHIMBAT pentru randarea initiala HTTP a paginii, ca sa nu riste nicio
    regresie pe testele deja existente ale acelui contract).

    Metricile derivate din ultima telemetrie bruta (`pv_power_kw` etc.)
    poarta `measured_at`/`received_at` reale ale masuratorii si calitatea
    per-rand (`data_quality`, identic cu `get_summary`); metricile derivate
    din stare evaluata "acum" (pret efectiv, plan, status device) poarta
    `measured_at`=acum si `received_at`=None (nu sunt masuratori telemetrice)."""
    latest = get_latest_telemetry(db, station.id)
    now = utcnow()

    data_quality = "missing"
    measured_at_iso: str | None = None
    received_at_iso: str | None = None
    if latest is not None:
        measured_at_iso = latest.measured_at.isoformat()
        received_at_iso = latest.received_at.isoformat()
        if latest.is_simulated:
            data_quality = "simulated"
        elif (now - latest.measured_at) > STALE_AFTER:
            data_quality = "stale"
        else:
            data_quality = "measured"

    import_tariff = tariff_service.get_current_tariff_version(db, station.id, "import", now)
    export_tariff = tariff_service.get_current_tariff_version(db, station.id, "export", now)
    market_price = db.scalar(
        select(MarketPriceInterval)
        .where(
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start <= now,
            MarketPriceInterval.interval_end > now,
        )
        .order_by(MarketPriceInterval.interval_start.desc())
        .limit(1)
    )
    price_buy = _effective_price(import_tariff, market_price)
    price_sell = _effective_price(export_tariff, market_price)

    plan = db.scalar(
        select(Plan)
        .where(Plan.station_id == station.id, Plan.status.in_([PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value]))
        .order_by(Plan.version.desc())
        .limit(1)
    )
    device_online = db.scalar(
        select(Device.id).where(Device.station_id == station.id, Device.last_heartbeat_at.isnot(None), Device.last_heartbeat_at > now - timedelta(minutes=5))
    ) is not None

    now_iso = now.isoformat()

    def _telemetry_metric(name: str, value, unit: str | None) -> dict:
        return {
            "metric": name, "value": value, "unit": unit,
            "measured_at": measured_at_iso, "received_at": received_at_iso,
            "quality": data_quality, "source": "telemetry",
        }

    def _evaluated_metric(name: str, value, unit: str | None, *, quality: str = "measured", source: str) -> dict:
        return {
            "metric": name, "value": value, "unit": unit,
            "measured_at": now_iso, "received_at": None,
            "quality": quality, "source": source,
        }

    return [
        _telemetry_metric("pv_power_kw", _w_to_kw(latest.pv_power_w) if latest else None, "kW"),
        _telemetry_metric("load_power_kw", _w_to_kw(latest.load_power_w) if latest else None, "kW"),
        _telemetry_metric("battery_power_kw", _w_to_kw(latest.battery_power_w) if latest else None, "kW"),
        _telemetry_metric("grid_power_kw", _w_to_kw(latest.grid_power_w) if latest else None, "kW"),
        _telemetry_metric(
            "battery_soc_percent",
            float(latest.battery_soc_percent) if latest and latest.battery_soc_percent is not None else None,
            "%",
        ),
        _telemetry_metric("ev_connected", latest.ev_connected if latest else None, None),
        _telemetry_metric("ev_power_kw", _w_to_kw(latest.ev_power_w) if latest else None, "kW"),
        # Duplica in `value` propriul `quality`/`measured_at` -- clientul (dashboard.js)
        # citeste aceste doua metrici pentru badge-ul global de prospetime,
        # separat de KPI-urile individuale de mai sus.
        _telemetry_metric("data_quality", data_quality, None),
        _telemetry_metric("last_update", measured_at_iso, None),
        # Provenienta ultimei telemetrii (issue #43) -- vezi `get_summary`
        # pentru semnificatia exacta (device_rs485/deye_cloud).
        _telemetry_metric("telemetry_source", latest.source if latest else None, None),
        _evaluated_metric("price_buy_lei_kwh", price_buy, "lei/kWh", quality="measured" if price_buy is not None else "missing", source="tariff"),
        _evaluated_metric("price_sell_lei_kwh", price_sell, "lei/kWh", quality="measured" if price_sell is not None else "missing", source="tariff"),
        _evaluated_metric("execution_mode", station.execution_mode, None, source="plan"),
        _evaluated_metric("has_active_plan", plan is not None, None, source="plan"),
        _evaluated_metric("plan_status", plan.status if plan else None, None, source="plan"),
        _evaluated_metric("device_online", device_online, None, source="device"),
    ]


def _w_to_kw(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value) / 1000.0


def _effective_price(tariff_version: TariffVersion | None, market_price: MarketPriceInterval | None) -> float | None:
    """Deleaga la `tariff_service.compute_effective_price_lei_per_kwh` (issue
    #46) -- SINGURA formula, include acum si distributie/transport/alte taxe
    reglementate/TVA, nu doar pret de energie + o componenta variabila
    generica. `float()` la iesire e doar pentru API-ul JSON al dashboard-ului;
    calculul insusi ramane Decimal in `tariff_service`."""
    market_price_lei_per_kwh = market_price.price_lei_per_kwh if market_price is not None else None
    result = tariff_service.compute_effective_price_lei_per_kwh(tariff_version, market_price_lei_per_kwh)
    return round(float(result), 6) if result is not None else None


def _tariff_versions_for_range(db: Session, station_id: uuid.UUID, direction: str, start: datetime, end: datetime) -> list[TariffVersion]:
    """Toate versiunile de tarif care se suprapun cu [start, end), ordonate
    dupa `valid_from` -- spre deosebire de `tariff_service.get_current_tariff_version`
    (un singur punct in timp, de regula "acum"), aici avem nevoie de fiecare
    versiune care a fost in vigoare o parte din interval, ca sa calculam
    costul istoric cu tariful REAL valabil in fiecare ora, nu cu cel curent."""
    return db.scalars(
        select(TariffVersion)
        .join(Tariff, Tariff.id == TariffVersion.tariff_id)
        .where(
            Tariff.station_id == station_id,
            Tariff.direction == direction,
            Tariff.is_active.is_(True),
            TariffVersion.valid_from < end,
        )
        .where((TariffVersion.valid_to.is_(None)) | (TariffVersion.valid_to > start))
        .order_by(TariffVersion.valid_from)
    ).all()


def _market_intervals_for_range(db: Session, source: str, start: datetime, end: datetime) -> list[MarketPriceInterval]:
    """`import_run` e incarcat eager (`selectinload`) -- necesar pentru
    `_price_provenance` (issue #49), care citeste `import_run.is_synthetic_fixture`
    pentru fiecare interval folosit; fara asta ar fi un N+1 lazy-load per ora."""
    return db.scalars(
        select(MarketPriceInterval)
        .options(selectinload(MarketPriceInterval.import_run))
        .where(
            MarketPriceInterval.source == source,
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start < end,
            MarketPriceInterval.interval_end > start,
        )
        .order_by(MarketPriceInterval.interval_start)
    ).all()


def _lookup_at(sorted_items: list, at: datetime, start_attr: str, end_attr: str):
    """Bisecteaza o lista deja sortata dupa `start_attr` si returneaza
    elementul care acopera `at` (start_attr <= at < end_attr), sau None daca
    niciunul nu acopera acel moment (gol de date, nu o presupunere gresita)."""
    starts = [getattr(x, start_attr) for x in sorted_items]
    idx = bisect.bisect_right(starts, at) - 1
    if idx < 0:
        return None
    item = sorted_items[idx]
    end_value = getattr(item, end_attr)
    if end_value is not None and at >= end_value:
        return None
    return item


def _effective_price_at(tariff_versions: list[TariffVersion], market_intervals: list[MarketPriceInterval], at: datetime) -> float | None:
    tariff = _lookup_at(tariff_versions, at, "valid_from", "valid_to")
    market = _lookup_at(market_intervals, at, "interval_start", "interval_end")
    return _effective_price(tariff, market)


def _price_provenance_at(tariff_versions: list[TariffVersion], market_intervals: list[MarketPriceInterval], at: datetime) -> str:
    """Clasifica provenienta pretului efectiv folosit intr-o ora (issue #49,
    "measured/modelled/estimated ... vizibile") -- NU introduce o sursa noua
    de date, doar citeste semnale deja existente in schema:

    - `"fixed_contract"`: tarif cu `fixed_price_lei_per_kwh` setat -- pretul e
      cunoscut EXACT din contract pentru orice ora, nu depinde de nicio
      prognoza sau piata (echivalent "measured", in sensul ca nu e o estimare).
    - `"indexed_settled"`: tarif indexat OPCOM, iar intervalul PZU folosit
      provine dintr-un `ImportRun` REAL (`is_synthetic_fixture=False`) -- pret
      decontat, masurat, nu modelat.
    - `"indexed_synthetic"`: tarif indexat OPCOM, dar intervalul PZU folosit
      provine dintr-un fixture sintetic (`is_synthetic_fixture=True`, date de
      test/demo injectate direct in baza, niciodata descarcate de la OPCOM) --
      marcat explicit ca NEFIIND un pret real decontat ("estimated").
    - `"unknown"`: niciun tarif/interval gasit pentru acest moment (nu ar
      trebui sa se intample pentru o ora deja inclusa ca "priced", dar
      returnat explicit in loc sa presupuna ceva)."""
    tariff = _lookup_at(tariff_versions, at, "valid_from", "valid_to")
    if tariff is None:
        return "unknown"
    if tariff.fixed_price_lei_per_kwh is not None:
        return "fixed_contract"
    market = _lookup_at(market_intervals, at, "interval_start", "interval_end")
    if market is None:
        return "unknown"
    return "indexed_synthetic" if market.import_run.is_synthetic_fixture else "indexed_settled"


def _query_telemetry_rows(db: Session, station: Station, start: datetime, end: datetime) -> list[dict]:
    """Randuri brute de telemetrie, cu `t` ca `datetime` (nu string inca) --
    folosit atat de exportul CSV (rezolutie bruta, neschimbata) cat si de
    agregarea pentru chart (issue #33), ca sa nu se duplice interogarea."""
    rows = db.scalars(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station.id, TelemetryRaw.measured_at >= start, TelemetryRaw.measured_at < end)
        .order_by(TelemetryRaw.measured_at)
    ).all()
    return [
        {
            "t": r.measured_at,
            "pv_kw": _w_to_kw(r.pv_power_w),
            "load_kw": _w_to_kw(r.load_power_w),
            "battery_kw": _w_to_kw(r.battery_power_w),
            "grid_kw": _w_to_kw(r.grid_power_w),
            "soc_pct": float(r.battery_soc_percent) if r.battery_soc_percent is not None else None,
            "data_quality": "simulated" if r.is_simulated else ("stale" if r.is_late else "measured"),
            "is_simulated": r.is_simulated,
            "is_late": r.is_late,
        }
        for r in rows
    ]


def get_timeseries_raw(db: Session, station: Station, start: datetime, end: datetime) -> list[dict]:
    """Telemetrie bruta (fara agregare), folosita de exportul CSV -- un
    export explicit e presupus sa vrea datele exact cum au fost masurate,
    nu o versiune redusa pentru afisare grafica."""
    rows = _query_telemetry_rows(db, station, start, end)
    for r in rows:
        r["t"] = r["t"].isoformat()
    return rows


def _query_aggregate_chart_rows(
    db: Session,
    station: Station,
    start: datetime,
    end: datetime,
    period_type: str,
) -> list[dict]:
    """Citeste rollup-urile persistate, astfel incat ferestrele de 7-365 zile
    sa nu materializeze in memoria workerului fiecare esantion brut."""
    aggregates = db.scalars(
        select(TelemetryAggregate)
        .where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == period_type,
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
        .order_by(TelemetryAggregate.period_start)
    ).all()

    def average_power(energy: Decimal | None, hours: float) -> float | None:
        return float(energy) / hours if energy is not None and hours > 0 else None

    result: list[dict] = []
    for row in aggregates:
        hours = (row.period_end - row.period_start).total_seconds() / 3600
        battery_kw = None
        if row.battery_charge_energy_kwh is not None and row.battery_discharge_energy_kwh is not None:
            battery_kw = (float(row.battery_charge_energy_kwh) - float(row.battery_discharge_energy_kwh)) / hours
        grid_kw = None
        if row.grid_import_energy_kwh is not None and row.grid_export_energy_kwh is not None:
            grid_kw = (float(row.grid_import_energy_kwh) - float(row.grid_export_energy_kwh)) / hours
        result.append(
            {
                "t": row.period_start,
                "pv_kw": average_power(row.pv_energy_kwh, hours),
                "load_kw": average_power(row.load_energy_kwh, hours),
                "battery_kw": battery_kw,
                "grid_kw": grid_kw,
                "soc_pct": float(row.avg_battery_soc_percent) if row.avg_battery_soc_percent is not None else None,
                "data_quality": row.data_quality,
                "is_simulated": row.data_quality == "simulated",
                "is_late": False,
            }
        )
    return result


_TIMESERIES_METRICS: dict[str, str] = {
    "pv_kw": "mean",
    "load_kw": "mean",
    "battery_kw": "mean",
    "grid_kw": "mean",
    # SOC e un procent -- NICIODATA insumat, doar mediat pe bucket. Vezi
    # `chart_aggregation.aggregate_series`, care ar refuza oricum "sum" aici.
    "soc_pct": "mean",
}

_QUALITY_RANK = {"measured": 0, "estimated": 1, "simulated": 2, "stale": 3, "missing": 4}


def _worse_quality(current: str, candidate: str | None) -> str:
    candidate = candidate or "missing"
    return candidate if _QUALITY_RANK.get(candidate, 4) > _QUALITY_RANK.get(current, 4) else current


def get_timeseries_chart(db: Session, station: Station, start: datetime, end: datetime, range_key: str) -> dict:
    """Seria pentru graficele de putere/SOC ale dashboard-ului (issue #33):
    rezolutie aleasa server-side dupa `range_key` (contract in
    `chart_aggregation.choose_resolution`), agregare metric-aware (medie
    pentru putere/SOC, niciodata suma pe SOC), plus metadate explicite
    (rezolutie, metoda de agregare per metrica, fus orar, acoperire) --
    clientul nu mai trebuie sa ghiceasca nimic din forma raspunsului."""
    resolution = chart_aggregation.choose_resolution(range_key)
    bucket_seconds = chart_aggregation.RESOLUTION_SECONDS[resolution]

    # 24h ramane pe raw pentru valori recente. Ferestrele lungi folosesc
    # rollup-urile create de aggregation_service: maximum ~672 randuri la 7d,
    # 720 la 30d si 365 la 1y, independent de frecventa telemetriei brute.
    # Randurile `day` sunt deja delimitate la miezul noptii locale a statiei
    # (inclusiv zile DST de 23/25h), deci nu le rebucketizam pe epoch UTC.
    source_period = {"7d": "interval_15m", "30d": "hour", "1y": "day"}.get(range_key)
    rows = (
        _query_aggregate_chart_rows(db, station, start, end, source_period)
        if source_period is not None
        else _query_telemetry_rows(db, station, start, end)
    )

    if range_key in ("30d", "1y"):
        # Sursa persistata are deja exact rezolutia ceruta (hour/day).
        aggregated = [{key: row[key] for key in ("t", *_TIMESERIES_METRICS)} for row in rows]
    else:
        aggregated = chart_aggregation.aggregate_series(
            rows, timestamp_key="t", bucket_seconds=bucket_seconds, metrics=_TIMESERIES_METRICS
        )
    coverage = chart_aggregation.compute_coverage(
        rows, timestamp_key="t", start=start, end=end, bucket_seconds=bucket_seconds
    )

    # Steaguri de calitate: "orice punct brut din bucket e simulat/intarziat"
    # -- pastrate separat de metricile numerice (nu au sens mediate).
    quality_by_bucket: dict[datetime, dict[str, bool | str]] = {}
    for row in rows:
        bucket_ts = (
            row["t"]
            if range_key in ("30d", "1y")
            else chart_aggregation.bucket_start(row["t"], bucket_seconds)
        )
        q = quality_by_bucket.setdefault(bucket_ts, {"is_simulated": False, "is_late": False, "data_quality": "measured"})
        q["is_simulated"] = q["is_simulated"] or bool(row["is_simulated"])
        q["is_late"] = q["is_late"] or bool(row["is_late"])
        q["data_quality"] = _worse_quality(str(q["data_quality"]), row.get("data_quality"))

    points = []
    for point in aggregated:
        bucket_ts = point["t"]
        q = quality_by_bucket.get(bucket_ts, {"is_simulated": False, "is_late": False, "data_quality": "missing"})
        points.append({**point, "t": bucket_ts.isoformat(), **q})

    return {
        "resolution": resolution,
        "aggregation": dict(_TIMESERIES_METRICS),
        "timezone": station.timezone,
        "coverage": round(coverage, 4),
        "points": points,
    }


def get_prices(db: Session, station: Station, day: date) -> list[dict]:
    tz = _station_tz(station)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    end = start + timedelta(days=1)
    rows = db.scalars(
        select(MarketPriceInterval)
        .where(
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start >= start,
            MarketPriceInterval.interval_start < end,
        )
        .order_by(MarketPriceInterval.interval_start)
    ).all()
    return [
        {
            "t": r.interval_start.astimezone(tz).isoformat(),
            "price_lei_kwh": float(r.price_lei_per_kwh),
            "is_negative": r.is_negative,
        }
        for r in rows
    ]


def get_plan_chart(db: Session, station: Station) -> dict:
    plan = db.scalar(
        select(Plan)
        .where(Plan.station_id == station.id, Plan.status.in_([PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value, PlanStatus.completed.value]))
        .order_by(Plan.version.desc())
        .limit(1)
    )
    if plan is None:
        return {"plan": None, "intervals": []}
    intervals = db.scalars(
        select(PlanInterval).where(PlanInterval.plan_id == plan.id).order_by(PlanInterval.interval_start)
    ).all()
    return {
        "plan": {"id": str(plan.id), "version": plan.version, "status": plan.status, "execution_mode": plan.execution_mode},
        "intervals": [
            {
                "t": pi.interval_start.isoformat(),
                "battery_kw": float(pi.battery_power_target_kw),
                "grid_kw": float(pi.grid_power_target_kw),
                "soc_target_pct": float(pi.battery_soc_target_percent),
                "explanation": pi.explanation,
                # Efectul REAL, completat ulterior dintr-un job de reconciliere (vezi
                # docstring-ul `OptimizationRun`/`Plan`) -- NU exista inca un asemenea
                # job in acest cod (vezi docs/LIMITATIONS.md), deci ramane None azi
                # pentru orice plan. Expus explicit, nu omis, ca planificat/executat
                # sa fie distincte de indata ce reconcilierea va exista.
                "observed_battery_kw": float(pi.observed_battery_power_kw) if pi.observed_battery_power_kw is not None else None,
                "observed_grid_kw": float(pi.observed_grid_power_kw) if pi.observed_grid_power_kw is not None else None,
                "observed_soc_pct": float(pi.observed_soc_percent) if pi.observed_soc_percent is not None else None,
                "deviation_notes": pi.deviation_notes,
            }
            for pi in intervals
        ],
    }


def get_energy_totals(db: Session, station: Station, granularity: str, periods: int) -> list[dict]:
    rows = db.scalars(
        select(TelemetryAggregate)
        .where(TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_type == granularity)
        .order_by(TelemetryAggregate.period_start.desc())
        .limit(periods)
    ).all()
    rows = list(reversed(rows))
    return [
        {
            "period_start": r.period_start.isoformat(),
            "pv_kwh": float(r.pv_energy_kwh) if r.pv_energy_kwh is not None else None,
            "load_kwh": float(r.load_energy_kwh) if r.load_energy_kwh is not None else None,
            "grid_import_kwh": float(r.grid_import_energy_kwh) if r.grid_import_energy_kwh is not None else None,
            "grid_export_kwh": float(r.grid_export_energy_kwh) if r.grid_export_energy_kwh is not None else None,
            "battery_charge_kwh": float(r.battery_charge_energy_kwh) if r.battery_charge_energy_kwh is not None else None,
            "battery_discharge_kwh": float(r.battery_discharge_energy_kwh) if r.battery_discharge_energy_kwh is not None else None,
            "data_quality": r.data_quality,
            "coverage": r.coverage,
        }
        for r in rows
    ]


def _period_start_utc(station: Station, period: str, now: datetime) -> datetime:
    tz = _station_tz(station)
    local_now = now.astimezone(tz)
    if period == "today":
        start_local = datetime(local_now.year, local_now.month, local_now.day, tzinfo=tz)
    elif period == "month":
        start_local = datetime(local_now.year, local_now.month, 1, tzinfo=tz)
    else:
        raise ValueError(f"Unsupported dashboard KPI period: {period}")
    return start_local.astimezone(UTC)


def _previous_period_start_utc(station: Station, period: str, now: datetime) -> datetime:
    tz = _station_tz(station)
    local_now = now.astimezone(tz)
    if period == "today":
        current = datetime(local_now.year, local_now.month, local_now.day, tzinfo=tz)
        return (current - timedelta(days=1)).astimezone(UTC)
    if period == "month":
        year = local_now.year
        month = local_now.month - 1
        if month == 0:
            year -= 1
            month = 12
        return datetime(year, month, 1, tzinfo=tz).astimezone(UTC)
    raise ValueError(f"Unsupported dashboard KPI period: {period}")


def _period_end_utc(station: Station, period: str, now: datetime) -> datetime:
    tz = _station_tz(station)
    local_now = now.astimezone(tz)
    if period == "today":
        next_day = date(local_now.year, local_now.month, local_now.day) + timedelta(days=1)
        return datetime.combine(next_day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    if period == "month":
        year, month = local_now.year, local_now.month
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return datetime(next_year, next_month, 1, tzinfo=tz).astimezone(UTC)
    raise ValueError(f"Unsupported dashboard KPI period: {period}")


_ENERGY_KPI_ENERGY_FIELDS = (
    "pv_energy_kwh", "load_energy_kwh", "grid_import_energy_kwh", "grid_export_energy_kwh",
    "battery_charge_energy_kwh", "battery_discharge_energy_kwh",
)
_ENERGY_KPI_COVERAGE_KEYS = ("pv", "load", "grid", "battery")


def _elapsed_window_totals(
    db: Session, station_id: uuid.UUID, source_period_type: str, window_start: datetime, window_end: datetime
) -> tuple[dict[str, Decimal | None], dict[str, float]]:
    """Suma + acoperirea fiecarei metrici energetice intr-o fereastra
    arbitrara [window_start, window_end), citita din agregatele de
    granularitate `source_period_type` -- o treapta mai fina decat perioada
    tinta (`interval_15m` pentru o fereastra de-o zi, `day` pentru o
    fereastra de-o luna). Folosita pentru comparatia "aceeasi durata scursa"
    ieri/luna trecuta: comparatia impotriva RANDULUI COMPLET al perioadei
    anterioare (comportamentul vechi) facea pragul de acoperire de 90%
    practic inatins pentru "azi" pana aproape de miezul noptii, pentru ca
    acoperirea unei zile in curs se calculeaza fata de ziua INTREAGA (24h),
    nu fata de cat a trecut deja din ea -- "comparatie cu ieri: indisponibila"
    aparea permanent, nu doar cand chiar lipseau date."""
    if window_end <= window_start:
        return {}, dict.fromkeys(_ENERGY_KPI_COVERAGE_KEYS, 0.0)
    rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station_id,
            TelemetryAggregate.period_type == source_period_type,
            TelemetryAggregate.period_start >= window_start,
            TelemetryAggregate.period_start < window_end,
        )
    ).all()
    total_seconds = (window_end - window_start).total_seconds()
    totals: dict[str, Decimal | None] = {}
    for field in _ENERGY_KPI_ENERGY_FIELDS:
        values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        totals[field] = Decimal(str(round(sum(float(v) for v in values), 6))) if values else None
    coverage: dict[str, float] = {}
    for key in _ENERGY_KPI_COVERAGE_KEYS:
        covered_seconds = sum(
            (min(r.period_end, window_end) - max(r.period_start, window_start)).total_seconds() * (r.coverage or {}).get(key, 0.0)
            for r in rows
        )
        coverage[key] = round(min(covered_seconds / total_seconds, 1.0), 4) if total_seconds > 0 else 0.0
    return totals, coverage


def _energy_kpi_value(
    row: TelemetryAggregate | None,
    field: str,
    coverage_key: str,
    current_elapsed_coverage: dict[str, float] | None = None,
    comparison_totals: dict[str, Decimal | None] | None = None,
    comparison_coverage: dict[str, float] | None = None,
) -> dict:
    current_elapsed_coverage = current_elapsed_coverage or {}
    comparison_totals = comparison_totals or {}
    comparison_coverage = comparison_coverage or {}
    if row is None:
        return {"value": None, "coverage": 0.0, "quality": "missing", "comparison": None}
    value = getattr(row, field)
    coverage = float((row.coverage or {}).get(coverage_key, 0) or 0)
    if value is None:
        return {"value": None, "coverage": coverage, "quality": "missing", "comparison": None}
    quality = "partial" if coverage < ENERGY_KPI_COVERAGE_PARTIAL_BELOW else row.data_quality
    comparison = None
    # Pragul se aplica pe acoperirea PORTIUNII DEJA SCURSE din perioada
    # curenta (nu pe `coverage`, relativa la perioada INTREAGA) -- altfel
    # "azi" nu ar trece niciodata pragul inainte de sfarsitul zilei.
    if current_elapsed_coverage.get(coverage_key, 0.0) >= ENERGY_KPI_COVERAGE_PARTIAL_BELOW:
        previous_value = comparison_totals.get(field)
        previous_coverage = comparison_coverage.get(coverage_key, 0.0)
        if previous_value is not None and previous_coverage >= ENERGY_KPI_COVERAGE_PARTIAL_BELOW:
            delta = value - previous_value
            comparison = {
                "previous_value": float(previous_value),
                "delta": float(delta),
                "delta_percent": float((delta / previous_value) * Decimal("100")) if previous_value != 0 else None,
            }
    return {"value": float(value), "coverage": round(coverage, 4), "quality": quality, "comparison": comparison}


def get_energy_period_kpis(db: Session, station: Station) -> dict:
    """KPI-uri client pentru ziua/luna curenta (issue #45).

    Agregatele sunt cautate dupa inceputul perioadei in calendarul statiei.
    Valoarea ramane `null` cand randul sau metrica lipseste: lipsa de date nu
    devine niciodata zero, iar acoperirea ramane atasata fiecarei metrici.

    Comparatia cu perioada anterioara ("ieri"/"luna anterioara") foloseste
    fereastra "aceeasi durata scursa" (vezi `_elapsed_window_totals`), NU
    randul complet al perioadei anterioare -- o zi/luna in curs nu poate fi
    comparata corect cu una INCHEIATA fara sa alunece pragul de acoperire
    dincolo de orice moment realist inainte de finalul perioadei.
    """
    now = utcnow()
    # (period_type-ul propriului rand, granularitatea sursa mai fina pentru
    # fereastra "aceeasi durata scursa")
    periods = {
        "today": ("day", "interval_15m", _period_start_utc(station, "today", now), _period_end_utc(station, "today", now)),
        "month": ("month", "day", _period_start_utc(station, "month", now), _period_end_utc(station, "month", now)),
    }
    rows = {
        name: db.scalar(
            select(TelemetryAggregate).where(
                TelemetryAggregate.station_id == station.id,
                TelemetryAggregate.period_type == period_type,
                TelemetryAggregate.period_start == period_start,
            )
        )
        for name, (period_type, _source_period_type, period_start, _period_end) in periods.items()
    }
    previous_starts = {
        "today": _previous_period_start_utc(station, "today", now),
        "month": _previous_period_start_utc(station, "month", now),
    }
    metric_fields = {
        "pv": ("pv_energy_kwh", "pv"),
        "load": ("load_energy_kwh", "load"),
        "grid_import": ("grid_import_energy_kwh", "grid"),
        "grid_export": ("grid_export_energy_kwh", "grid"),
        "battery_charge": ("battery_charge_energy_kwh", "battery"),
        "battery_discharge": ("battery_discharge_energy_kwh", "battery"),
    }
    out = {}
    for name, (period_type, source_period_type, period_start, period_end) in periods.items():
        row = rows[name]
        elapsed_seconds = max((now - period_start).total_seconds(), 0.0)
        period_seconds = (period_end - period_start).total_seconds()
        current_elapsed_coverage = {
            key: (
                min((float((row.coverage or {}).get(key, 0) or 0) * period_seconds) / elapsed_seconds, 1.0)
                if row is not None and elapsed_seconds > 0
                else 0.0
            )
            for key in _ENERGY_KPI_COVERAGE_KEYS
        }
        comparison_totals, comparison_coverage = _elapsed_window_totals(
            db, station.id, source_period_type, previous_starts[name], previous_starts[name] + timedelta(seconds=elapsed_seconds)
        )
        out[name] = {
            "period_start": period_start.isoformat(),
            "period_type": period_type,
            "comparison_label": "ieri" if name == "today" else "luna anterioara",
            "metrics": {
                metric: _energy_kpi_value(row, field, coverage_key, current_elapsed_coverage, comparison_totals, comparison_coverage)
                for metric, (field, coverage_key) in metric_fields.items()
            },
        }
    return out


def get_heatmap(db: Session, station: Station, weeks: int = 8) -> list[dict]:
    tz = _station_tz(station)
    since = utcnow() - timedelta(weeks=weeks)
    rows = db.scalars(
        select(TelemetryAggregate)
        .where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start >= since,
        )
    ).all()
    buckets: dict[tuple[int, int], list[float]] = {}
    for r in rows:
        if r.load_energy_kwh is None or (r.coverage or {}).get("load", 0) < 0.9:
            continue
        local = r.period_start.astimezone(tz)
        slot = local.hour * 4 + local.minute // 15
        buckets.setdefault((local.weekday(), slot), []).append(float(r.load_energy_kwh) * 4)
    return [
        {"weekday": wd, "slot": slot, "avg_load_kw": sum(v) / len(v)}
        for (wd, slot), v in sorted(buckets.items())
    ]


def get_efc_used(db: Session, station: Station, config: StationConfigVersion | None, start: datetime, end: datetime) -> float | None:
    if config is None or not config.battery_reference_capacity_kwh:
        return None
    total_discharge = db.scalar(
        select(func.coalesce(func.sum(TelemetryAggregate.battery_discharge_energy_kwh), 0)).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "hour",
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
    )
    return float(total_discharge) / float(config.battery_reference_capacity_kwh)


def get_forecast_vs_actual(db: Session, station: Station, metric: str, start: datetime, end: datetime) -> list[dict]:
    """Fiecare prognoza e regenerata periodic (batch-uri noi cu `issued_at` mai
    recent), iar randurile vechi raman in baza -- fara sa alegem explicit,
    interogarea de mai jos ar returna MAI MULTE randuri pentru acelasi
    `interval_start` (unul per batch), amestecand pe grafic o prognoza veche,
    deja depasita, cu una noua. Pentru intervale istorice pastram prognoza cea
    mai RECENTA care exista deja LA MOMENTUL acelui interval
    (`issued_at <= interval_start`) -- "prognoza asa cum era cunoscuta atunci",
    nu una regenerata ulterior. Pentru intervale viitoare folosim ultimul batch
    disponibil, ca seria afisata sa fie continua si sa nu alterneze artificial
    intre valori valide si lipsa de prognoza."""
    from app.models.forecast import ConsumptionForecast, PvForecast, WeatherForecast

    aggregates = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start < end,
            TelemetryAggregate.period_end > start,
        )
    ).all()

    def actual_average_kw(interval_start: datetime, interval_end: datetime) -> float | None:
        total_seconds = (interval_end - interval_start).total_seconds()
        if total_seconds <= 0:
            return None
        energy_total = Decimal("0")
        covered_seconds = 0.0
        for actual in aggregates:
            overlap_start = max(actual.period_start, interval_start)
            overlap_end = min(actual.period_end, interval_end)
            overlap_seconds = (overlap_end - overlap_start).total_seconds()
            if overlap_seconds <= 0:
                continue
            energy = actual.pv_energy_kwh if metric == "pv" else actual.load_energy_kwh
            metric_coverage = float((actual.coverage or {}).get(metric, 0) or 0)
            if energy is None or metric_coverage <= 0:
                continue
            aggregate_seconds = (actual.period_end - actual.period_start).total_seconds()
            if aggregate_seconds <= 0:
                continue
            energy_total += energy * Decimal(str(overlap_seconds / aggregate_seconds))
            covered_seconds += overlap_seconds * metric_coverage
        if covered_seconds / total_seconds < 0.9:
            return None
        return float(energy_total) / (total_seconds / 3600)

    if metric == "pv":
        rows = db.scalars(
            select(PvForecast).where(
                PvForecast.station_id == station.id,
                PvForecast.scenario == "expected",
                PvForecast.interval_start >= start,
                PvForecast.interval_start < end,
            )
        ).all()
    else:
        rows = db.scalars(
            select(ConsumptionForecast).where(
                ConsumptionForecast.station_id == station.id,
                ConsumptionForecast.interval_start >= start,
                ConsumptionForecast.interval_start < end,
            )
        ).all()

    now = utcnow()
    latest_issued_at = max((f.issued_at for f in rows), default=None)
    best_by_start: dict[datetime, object] = {}
    for f in rows:
        if f.interval_start <= now and f.issued_at > f.interval_start:
            continue  # prognoza emisa "dupa" momentul prezis -- nu era disponibila atunci
        if f.interval_start > now and latest_issued_at is not None and f.issued_at < latest_issued_at:
            continue  # pentru viitor afisam ultimul batch disponibil, nu batch-uri vechi amestecate
        existing = best_by_start.get(f.interval_start)
        if existing is None or f.issued_at > existing.issued_at:
            best_by_start[f.interval_start] = f
    forecasts = sorted(best_by_start.values(), key=lambda f: f.interval_start)

    weather_by_id: dict[uuid.UUID, WeatherForecast] = {}
    if metric == "pv":
        weather_ids = [f.based_on_weather_forecast_id for f in forecasts if f.based_on_weather_forecast_id is not None]
        if weather_ids:
            weather_by_id = {
                w.id: w for w in db.scalars(select(WeatherForecast).where(WeatherForecast.id.in_(weather_ids))).all()
            }

    out = []
    for f in forecasts:
        actual_kw = actual_average_kw(f.interval_start, f.interval_end)
        forecast_kw = float(f.predicted_power_kw) if metric == "pv" else float(f.base_load_kw + f.ev_component_kw + f.flexible_component_kw)
        point = {
            "t": f.interval_start.isoformat(),
            "forecast_kw": forecast_kw,
            "actual_kw": actual_kw,
            "is_synthetic": f.is_synthetic,
            "forecast_confidence": f.confidence,
            "forecast_source": f.source,
            "forecast_source_version": f.source_version,
            "forecast_issued_at": f.issued_at.isoformat(),
        }
        if metric == "pv":
            weather = weather_by_id.get(f.based_on_weather_forecast_id)
            point["weather"] = None if weather is None else {
                "source": weather.source,
                "source_version": weather.source_version,
                "issued_at": weather.issued_at.isoformat(),
                "ghi_w_m2": weather.ghi_w_m2,
                "dni_w_m2": weather.dni_w_m2,
                "dhi_w_m2": weather.dhi_w_m2,
                "cloud_cover_percent": weather.cloud_cover_percent,
                "temperature_c": weather.temperature_c,
                "precipitation_mm": weather.precipitation_mm,
                "wind_speed_ms": weather.wind_speed_ms,
                "confidence": weather.confidence,
                "is_synthetic": weather.is_synthetic,
            }
        out.append(point)
    return out


def get_estimated_savings(db: Session, station: Station, start: datetime, end: datetime) -> dict:
    """Economie estimata fata de DOUA repere explicite si distincte, fiecare
    documentat in UI (nu prezentate ca economie masurata):

    1. Beneficiul intregului sistem PV/baterie: cost real (import - export, la
       preturile REALE valabile istoric, nu tariful curent) fata de costul
       ipotetic daca TOT consumul ar fi fost importat din retea, fara PV/baterie.
    2. Beneficiul INCREMENTAL al EMS (optimizarea activa): cost real fata de
       un al doilea reper -- PV si baterie INSTALATE, dar fara optimizare
       activa (auto-consum direct: surplusul PV se exporta, deficitul se
       importa, fara arbitraj de pret). Izoleaza contributia proprie a
       platformei de beneficiul pe care l-ar aduce oricum orice PV/baterie.

    Ambele repere folosesc tariful REAL valabil in FIECARE ORA a intervalului
    (nu tariful curent aplicat retroactiv intregului istoric -- bug corectat
    fata de versiunea anterioara). Orele fara pret rezolvabil sunt EXCLUSE
    din toate cele trei sume (real/reper1/reper2), nu tratate ca zero, iar
    acoperirea ramasa e raportata explicit (`hours_priced`/`hours_expected`).

    Issue #49 adauga o DETALIERE financiara suplimentara, in carduri separate,
    ca sa nu fie confundata cu "economie totala": `gross_pv_value_lei`
    (valoare bruta = productie PV x pret cumparare, cost potential evitat, NU
    economie realizata), `self_consumption_savings_lei` (economie REALA prin
    autoconsum direct) si `export_revenue_lei` (venit real din export -- deja
    parte din `actual_net_cost_lei`, expus separat). Fiecare are `_description`
    cu formula exacta. `tariff_buy_provenance`/`tariff_provenance_summary`
    expun daca pretul de import folosit e masurat (`fixed_contract`/
    `indexed_settled`) sau doar un fixture sintetic de test (`indexed_synthetic`,
    `tariff_provenance_summary="estimated"`)."""
    tariff_versions_buy = _tariff_versions_for_range(db, station.id, "import", start, end)
    if not tariff_versions_buy:
        return {"available": False, "reason": "Niciun tarif de import valabil in intervalul cerut."}
    tariff_versions_sell = _tariff_versions_for_range(db, station.id, "export", start, end)
    market_intervals = _market_intervals_for_range(db, "opcom_pzu", start, end)

    rows = db.scalars(
        select(TelemetryAggregate)
        .where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "hour",
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
        .order_by(TelemetryAggregate.period_start)
    ).all()

    hours_expected = max(int((end - start).total_seconds() / 3600), 0)
    hours_priced = 0
    hours_with_load_but_no_price = 0
    hours_with_incomplete_energy_data = 0
    hours_export_price_missing = 0
    total_load_kwh = 0.0
    total_pv_kwh = 0.0
    actual_net_cost = 0.0
    whole_system_baseline_cost = 0.0
    ems_incremental_baseline_cost = 0.0
    gross_pv_value = 0.0
    self_consumption_savings = 0.0
    export_revenue = 0.0
    tariff_buy_provenance = {"fixed_contract": 0, "indexed_settled": 0, "indexed_synthetic": 0, "unknown": 0}

    for r in rows:
        # NULL inseamna necunoscut in TelemetryAggregate. Un calcul financiar
        # necesita toate fluxurile; inlocuirea oricaruia cu zero ar fabrica o
        # economie sau un venit care nu a fost masurat.
        if any(
            value is None
            for value in (
                r.load_energy_kwh,
                r.pv_energy_kwh,
                r.grid_import_energy_kwh,
                r.grid_export_energy_kwh,
            )
        ):
            hours_with_incomplete_energy_data += 1
            continue
        price_buy = _effective_price_at(tariff_versions_buy, market_intervals, r.period_start)
        if price_buy is None:
            hours_with_load_but_no_price += 1
            continue

        load = float(r.load_energy_kwh)
        pv = float(r.pv_energy_kwh)
        grid_import = float(r.grid_import_energy_kwh)
        grid_export = float(r.grid_export_energy_kwh)
        self_export = max(pv - load, 0.0)

        # Pretul de export este obligatoriu numai daca scenariul real sau
        # baseline-ul au export. Daca lipseste, excludem ora din toate sumele
        # comparabile; necunoscutul nu devine venit zero.
        price_sell_resolved = _effective_price_at(tariff_versions_sell, market_intervals, r.period_start)
        if price_sell_resolved is None and (grid_export > 0.0 or self_export > 0.0):
            hours_export_price_missing += 1
            continue
        price_sell = price_sell_resolved if price_sell_resolved is not None else 0.0

        hours_priced += 1
        total_load_kwh += load
        total_pv_kwh += pv
        actual_net_cost += grid_import * price_buy - grid_export * price_sell
        whole_system_baseline_cost += load * price_buy

        self_import = max(load - pv, 0.0)
        ems_incremental_baseline_cost += self_import * price_buy - self_export * price_sell

        # Detaliere issue #49: NU sunt trei numere independente insumabile la
        # `whole_system_benefit_lei` (acela foloseste fluxurile REALE de retea,
        # care depind si de baterie) -- fiecare e etichetat explicit cu
        # formula/reperul lui propriu, ca sa nu fie confundat cu "economie totala".
        gross_pv_value += pv * price_buy
        self_consumption_savings += min(pv, load) * price_buy
        export_revenue += grid_export * price_sell

        tariff_buy_provenance[_price_provenance_at(tariff_versions_buy, market_intervals, r.period_start)] += 1

    if hours_priced == 0:
        return {"available": False, "reason": "Nicio ora cu date de consum si pret rezolvabil in intervalul cerut."}

    has_synthetic_buy_price = tariff_buy_provenance["indexed_synthetic"] > 0
    tariff_provenance_summary = "estimated" if has_synthetic_buy_price else "measured"

    return {
        "available": True,
        "hours_priced": hours_priced,
        "hours_expected": hours_expected,
        "hours_with_load_but_no_price": hours_with_load_but_no_price,
        "hours_with_incomplete_energy_data": hours_with_incomplete_energy_data,
        "coverage_ratio": round(hours_priced / hours_expected, 4) if hours_expected else None,
        "total_load_kwh": round(total_load_kwh, 3),
        "actual_net_cost_lei": round(actual_net_cost, 2),
        "whole_system_baseline_description": (
            "Cost ipotetic daca tot consumul era importat din retea, fara PV/baterie -- "
            "la tariful REAL valabil in fiecare ora din interval, nu tariful curent."
        ),
        "whole_system_baseline_cost_lei": round(whole_system_baseline_cost, 2),
        "whole_system_benefit_lei": round(whole_system_baseline_cost - actual_net_cost, 2),
        "ems_incremental_baseline_description": (
            "Cost ipotetic cu PV si baterie instalate, dar FARA optimizare activa "
            "(auto-consum direct: surplusul PV se exporta, deficitul se importa, fara "
            "arbitraj de pret sau incarcare/descarcare programata). Aproximare simpla, "
            "documentata -- nu o simulare completa a unui sistem PV/baterie fara EMS."
        ),
        "ems_incremental_baseline_cost_lei": round(ems_incremental_baseline_cost, 2),
        "ems_incremental_benefit_lei": round(ems_incremental_baseline_cost - actual_net_cost, 2),
        # --- Detaliere financiara (issue #49) ------------------------------
        # Cele trei numere de mai jos sunt afisate ca CARDURI SEPARATE, fiecare
        # cu formula lui, exact ca sa NU fie prezentate implicit ca "economie
        # totala" (definitia explicita din issue #49: pret contractual x
        # productie PV e valoare bruta/cost evitat POTENTIAL, nu automat
        # economie realizata -- autoconsumul, exportul si baseline-ul conteaza).
        "total_pv_kwh": round(total_pv_kwh, 3),
        "gross_pv_value_description": (
            "Valoare bruta a energiei PV produse = productie PV (kWh) x pretul de "
            "cumparare efectiv (lei/kWh) valabil in fiecare ora -- costul de cumparare "
            "POTENTIAL evitat daca toata productia ar fi fost cumparata din retea. "
            "NU e economie realizata: nu tine cont daca PV-ul a fost efectiv "
            "autoconsumat, exportat sau curbat (curtailed)."
        ),
        "gross_pv_value_lei": round(gross_pv_value, 2),
        "self_consumption_savings_description": (
            "Economie din autoconsum = min(PV, consum) (kWh) x pretul de cumparare "
            "efectiv (lei/kWh), pe fiecare ora -- costul de import EVITAT prin "
            "consumul direct al energiei PV produse in acea ora. Aproximare orara "
            "(nu tine cont de decalaje in cadrul orei intre productie si consum)."
        ),
        "self_consumption_savings_lei": round(self_consumption_savings, 2),
        "export_revenue_description": (
            "Venit din export = energie exportata in retea (kWh) x pretul de export "
            "efectiv (lei/kWh) valabil in acea ora -- venit REAL, deja inclus in "
            "`actual_net_cost_lei` (il reduce), afisat aici separat pentru claritate."
        ),
        "export_revenue_lei": round(export_revenue, 2),
        "hours_export_price_missing": hours_export_price_missing,
        "tariff_buy_provenance": tariff_buy_provenance,
        "tariff_buy_provenance_description": (
            "Cate din orele cu pret rezolvabil au folosit un pret de import "
            "'fixed_contract' (cunoscut exact din contract), 'indexed_settled' "
            "(pret PZU OPCOM real, decontat) sau 'indexed_synthetic' (date de "
            "test/demo, NU un pret OPCOM real decontat)."
        ),
        "tariff_provenance_summary": tariff_provenance_summary,
    }
