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
from app.services import tariff_service

STALE_AFTER = timedelta(minutes=10)


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


def get_timeseries(db: Session, station: Station, start: datetime, end: datetime) -> list[dict]:
    rows = db.scalars(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station.id, TelemetryRaw.measured_at >= start, TelemetryRaw.measured_at <= end)
        .order_by(TelemetryRaw.measured_at)
    ).all()
    return [
        {
            "t": r.measured_at.isoformat(),
            "pv_kw": _w_to_kw(r.pv_power_w),
            "load_kw": _w_to_kw(r.load_power_w),
            "battery_kw": _w_to_kw(r.battery_power_w),
            "grid_kw": _w_to_kw(r.grid_power_w),
            "soc_pct": float(r.battery_soc_percent) if r.battery_soc_percent is not None else None,
            "is_simulated": r.is_simulated,
            "is_late": r.is_late,
        }
        for r in rows
    ]


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


def get_heatmap(db: Session, station: Station, weeks: int = 8) -> list[dict]:
    tz = _station_tz(station)
    since = utcnow() - timedelta(weeks=weeks)
    rows = db.scalars(
        select(TelemetryAggregate)
        .where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "hour",
            TelemetryAggregate.period_start >= since,
        )
    ).all()
    buckets: dict[tuple[int, int], list[float]] = {}
    for r in rows:
        if r.load_energy_kwh is None or (r.coverage or {}).get("load", 0) < 0.9:
            continue
        local = r.period_start.astimezone(tz)
        key = (local.weekday(), local.hour)
        buckets.setdefault(key, []).append(float(r.load_energy_kwh))
    return [
        {"weekday": wd, "hour": h, "avg_load_kwh": sum(v) / len(v)}
        for (wd, h), v in sorted(buckets.items())
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
    deja depasita, cu una noua. Pentru fiecare `interval_start`, pastram doar
    prognoza cea mai RECENTA care exista deja LA MOMENTUL acelui interval
    (`issued_at <= interval_start`) -- "prognoza asa cum era cunoscuta atunci",
    nu una regenerata ulterior (ar insemna folosirea retroactiva a unei
    informatii care inca nu exista la acel moment)."""
    from app.models.forecast import ConsumptionForecast, PvForecast

    aggregates = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
    ).all()
    actual_by_start = {a.period_start: a for a in aggregates}

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

    best_by_start: dict[datetime, object] = {}
    for f in rows:
        if f.issued_at > f.interval_start:
            continue  # prognoza emisa "dupa" momentul prezis -- nu era disponibila atunci
        existing = best_by_start.get(f.interval_start)
        if existing is None or f.issued_at > existing.issued_at:
            best_by_start[f.interval_start] = f
    forecasts = sorted(best_by_start.values(), key=lambda f: f.interval_start)

    out = []
    for f in forecasts:
        actual = actual_by_start.get(f.interval_start)
        actual_kw = None
        if actual is not None:
            energy = actual.pv_energy_kwh if metric == "pv" else actual.load_energy_kwh
            actual_kw = float(energy) * 4 if energy is not None and (actual.coverage or {}).get(metric, 0) >= 0.9 else None  # kWh pe interval de 15 min -> kW mediu
        forecast_kw = float(f.predicted_power_kw) if metric == "pv" else float(f.base_load_kw + f.ev_component_kw + f.flexible_component_kw)
        out.append({"t": f.interval_start.isoformat(), "forecast_kw": forecast_kw, "actual_kw": actual_kw, "is_synthetic": f.is_synthetic})
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
        if r.load_energy_kwh is None:
            continue
        price_buy = _effective_price_at(tariff_versions_buy, market_intervals, r.period_start)
        if price_buy is None:
            hours_with_load_but_no_price += 1
            continue
        # Nicio ipoteza de venit necunoscut daca nu exista tarif de export valabil
        # in acea ora -- la fel ca `optimization_service._resolve_price`.
        price_sell_resolved = _effective_price_at(tariff_versions_sell, market_intervals, r.period_start)
        if price_sell_resolved is None:
            hours_export_price_missing += 1
        price_sell = price_sell_resolved or 0.0

        load = float(r.load_energy_kwh)
        pv = float(r.pv_energy_kwh) if r.pv_energy_kwh is not None else 0.0
        grid_import = float(r.grid_import_energy_kwh) if r.grid_import_energy_kwh is not None else 0.0
        grid_export = float(r.grid_export_energy_kwh) if r.grid_export_energy_kwh is not None else 0.0

        hours_priced += 1
        total_load_kwh += load
        total_pv_kwh += pv
        actual_net_cost += grid_import * price_buy - grid_export * price_sell
        whole_system_baseline_cost += load * price_buy

        self_import = max(load - pv, 0.0)
        self_export = max(pv - load, 0.0)
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
