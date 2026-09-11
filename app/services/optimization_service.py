"""Motorul de optimizare: orizont 24-48h, intervale de 15 minute, Pyomo + HiGHS.

Separarea ceruta de specificatie:
  OptimizationRun (scenariul calculat, cu input_snapshot pt. reproductibilitate)
    -> Plan (planul publicat, daca run-ul a reusit) -- status/executie separate
        -> acceptarea de catre dispozitiv, comenzile si confirmarile de executie
           se gestioneaza in alta parte (device_service / API v1)
        -> efectul observat se completeaza ulterior dintr-un job de reconciliere
           (PlanInterval.observed_*), comparand cu telemetria reala.

Modul shadow (implicit): planul e calculat si afisat, dar `execution_mode`
ramane "shadow" -- nu autorizeaza executia fizica. Comutarea la "live" e o
actiune administrativa explicita asupra statiei.

Strategie de fallback conservatoare: daca lipsesc complet prognozele (PV si
consum) sau solverul esueaza/timeout/infezabil, NU se publica un plan bazat pe
presupuneri riscante -- se publica un plan de asteptare (baterie in hold,
retea pass-through), cu motivul documentat in `fallback_reason`.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pyomo.environ as pyo
import structlog
from pyomo.opt import TerminationCondition
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.rate_limit import get_redis
from app.core.security import utcnow
from app.models.enums import ExecutionMode, OptimizationRunStatus, PlanStatus
from app.models.forecast import ConsumptionForecast, PvForecast
from app.models.market import ImportRun, MarketPriceInterval
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.preference import PreferenceVersion
from app.models.station import Station, StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.services import (
    consumption_forecast_service,
    pv_forecast_service,
    tariff_service,
    weather_service,
)
from app.services.dashboard_service import get_efc_used

logger = structlog.get_logger(__name__)
settings = get_settings()

DEFAULT_BATTERY_WEAR_COST_LEI_PER_KWH = Decimal("0.05")
SOC_TARGET_PENALTY_LEI_PER_KWH = 2.0
EV_SHORTFALL_PENALTY_LEI_PER_KWH = 5.0
PRIORITY_WEIGHTS = {
    "cost": {"soc_target": 1.0, "ev": 1.0, "wear": 1.0, "terminal_value": 1.0},
    "autonomy": {"soc_target": 1.5, "ev": 1.3, "wear": 0.8, "terminal_value": 2.0},
    "battery_protection": {"soc_target": 0.8, "ev": 0.8, "wear": 2.5, "terminal_value": 1.0},
}


class OptimizationLockedError(Exception):
    pass


def _acquire_lock(station_id: uuid.UUID):
    r = get_redis()
    lock = r.lock(f"optimization_lock:{station_id}", timeout=120, blocking_timeout=1)
    if not lock.acquire(blocking=True):
        raise OptimizationLockedError("Exista deja o optimizare in curs pentru aceasta statie.")
    return lock


def _round_to_interval(dt: datetime, minutes: int) -> datetime:
    discard = timedelta(minutes=dt.minute % minutes, seconds=dt.second, microseconds=dt.microsecond)
    return dt - discard


def _current_soc_kwh(db: Session, station: Station, available_capacity_kwh: Decimal) -> tuple[float, datetime | None, str]:
    """Returneaza (soc_kwh, momentul masuratorii, calitate), calitate fiind
    "measured" (proaspata), "stale" (mai veche decat pragul configurat, dar
    folosita ca fiind cea mai buna informatie disponibila) sau "missing" (nicio
    telemetrie SOC inregistrata vreodata -- valoarea e o presupunere de mijloc
    de banda, NU o masuratoare). Apelantul decide daca o calitate sub
    "measured" blocheaza planul LIVE; aici nu se ascunde lipsa datelor."""
    latest = db.scalar(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station.id, TelemetryRaw.battery_soc_percent.isnot(None))
        .order_by(TelemetryRaw.measured_at.desc())
        .limit(1)
    )
    if latest is None:
        return 0.5 * float(available_capacity_kwh), None, "missing"

    soc_kwh = float(latest.battery_soc_percent) / 100.0 * float(available_capacity_kwh)
    max_age = timedelta(minutes=settings.optimization_soc_max_age_minutes)
    quality = "measured" if (utcnow() - latest.measured_at) <= max_age else "stale"
    return soc_kwh, latest.measured_at, quality


def _build_pv_series(db: Session, station_id: uuid.UUID, horizon: list[datetime]) -> dict[datetime, float]:
    latest_issued = db.scalar(
        select(PvForecast.issued_at)
        .where(PvForecast.station_id == station_id, PvForecast.scenario == "expected")
        .order_by(PvForecast.issued_at.desc())
        .limit(1)
    )
    if latest_issued is None:
        return dict.fromkeys(horizon)
    rows = db.scalars(
        select(PvForecast).where(
            PvForecast.station_id == station_id, PvForecast.issued_at == latest_issued, PvForecast.scenario == "expected"
        )
    ).all()
    series = {}
    for t in horizon:
        match = next((r for r in rows if r.interval_start <= t < r.interval_end), None)
        series[t] = float(match.predicted_power_kw) if match else None
    return series


def _build_load_series(db: Session, station_id: uuid.UUID, horizon: list[datetime]) -> dict[datetime, float]:
    latest_issued = db.scalar(
        select(ConsumptionForecast.issued_at)
        .where(ConsumptionForecast.station_id == station_id)
        .order_by(ConsumptionForecast.issued_at.desc())
        .limit(1)
    )
    if latest_issued is None:
        return dict.fromkeys(horizon)
    rows = db.scalars(
        select(ConsumptionForecast).where(
            ConsumptionForecast.station_id == station_id, ConsumptionForecast.issued_at == latest_issued
        )
    ).all()
    by_start = {r.interval_start: float(r.base_load_kw + r.ev_component_kw + r.flexible_component_kw) for r in rows}
    return {t: by_start.get(t) for t in horizon}


def _fill_gaps(series: dict[datetime, float | None], fallback: float) -> dict[datetime, float]:
    out = {}
    last = None
    for t, v in series.items():
        if v is not None:
            last = v
            out[t] = v
        else:
            out[t] = last if last is not None else fallback
    return out


def _fill_price_gaps(prices: dict[datetime, float | None]) -> tuple[dict[datetime, float], dict[datetime, str]]:
    """Completeaza golurile de pret prin propagare din cel mai apropiat interval
    cunoscut in timp (inainte, apoi -- pentru golul initial -- inapoi), nu dintr-o
    valoare oarecare disponibila oriunde in orizont. Fiecare interval e marcat
    explicit "real" sau "estimated"; apelantul foloseste aceasta calitate pentru
    a decide daca planul poate ramane live sau trebuie retrogradat la shadow.
    Presupune ca cel putin o valoare reala exista (verificat de apelant)."""
    keys = list(prices.keys())
    filled: dict[datetime, float | None] = {}
    quality: dict[datetime, str] = {}
    last_known: float | None = None
    for t in keys:
        v = prices[t]
        if v is not None:
            filled[t] = v
            quality[t] = "real"
            last_known = v
        else:
            filled[t] = last_known
            quality[t] = "estimated"

    next_known: float | None = None
    for t in reversed(keys):
        if quality[t] == "real":
            next_known = filled[t]
        elif filled[t] is None:
            filled[t] = next_known

    return filled, quality


def _ensure_forecasts(db: Session, station: Station, horizon_start: datetime, horizon_end: datetime) -> None:
    """Best-effort: reimprospateaza meteo/PV/consum. Esecurile sunt tolerate --
    optimizatorul foloseste orice date existente deja in baza, iar lipsa totala
    declanseaza fallback-ul conservator mai jos. Fiecare incercare ruleaza intr-un
    SAVEPOINT dedicat: o exceptie (ex. o constrangere DB in timpul flush-ului)
    invalideaza altfel intreaga tranzactie SQLAlchemy pana la rollback, ceea ce
    ar sterge si `OptimizationRun`-ul deja adaugat de apelant in aceeasi sesiune
    necomisa -- SAVEPOINT-ul limiteaza rollback-ul strict la incercarea esuata.

    `horizon_start`/`horizon_end` sunt granitele deja aliniate la grila UTC a
    orizontului de optimizare (vezi `_round_to_interval` in apelant) -- consumul
    e generat pe pasi ficsi de 15 minute incepand exact de la `horizon_start`,
    deci nealinierea acestor granite ar face ca niciun interval generat sa nu
    se potriveasca vreodata cu orele cautate de `_build_load_series`."""
    try:
        with db.begin_nested():
            weather_service.refresh_weather_for_station(db, station)
    except Exception as exc:
        logger.info("optimization.weather_refresh_skipped", station_id=str(station.id), reason=str(exc))

    try:
        with db.begin_nested():
            pv_forecast_service.generate_pv_forecast(db, station)
    except Exception as exc:
        logger.info("optimization.pv_forecast_skipped", station_id=str(station.id), reason=str(exc))

    try:
        with db.begin_nested():
            consumption_forecast_service.generate_consumption_forecast(db, station, horizon_start, horizon_end)
    except Exception as exc:
        logger.info("optimization.consumption_forecast_skipped", station_id=str(station.id), reason=str(exc))


def _explain_interval(pi_data: dict, priority: str) -> str:
    batt = pi_data["battery_power_target_kw"]
    grid = pi_data["grid_power_target_kw"]
    price = pi_data.get("price_import_lei_kwh")

    if abs(batt) < 0.01 and abs(grid) < 0.01:
        return "Sistem in echilibru: productia PV acopera consumul, fara actiune asupra bateriei sau retelei."
    parts = []
    if batt > 0.01:
        parts.append(f"Se incarca bateria cu {batt:.2f} kW")
        if grid > 0.01:
            parts.append(f"folosind si import din retea ({grid:.2f} kW)")
        else:
            parts.append("din surplusul de productie PV")
    elif batt < -0.01:
        parts.append(f"Se descarca bateria cu {abs(batt):.2f} kW pentru a acoperi consumul")
        if grid > 0.01:
            parts.append(f"si se importa suplimentar {grid:.2f} kW")

    if abs(batt) < 0.01 and grid > 0.01:
        parts.append(f"Se importa {grid:.2f} kW din retea (productia PV si bateria nu acopera consumul)")
    elif abs(batt) < 0.01 and grid < -0.01:
        parts.append(f"Se exporta {abs(grid):.2f} kW in retea (surplus PV peste consum si limitele bateriei)")

    if price is not None and grid > 0.01:
        parts.append(f"la un pret de import de {price:.4f} lei/kWh")

    sentence = " ".join(parts) if parts else "Actiune neutra."
    priority_note = {
        "autonomy": " (prioritate: autonomie fata de retea)",
        "battery_protection": " (prioritate: protejarea bateriei)",
        "cost": "",
    }.get(priority, "")
    return sentence.strip() + priority_note + "."


def run_optimization_for_station(db: Session, station_id: uuid.UUID, triggered_by: str, triggered_by_user_id=None) -> OptimizationRun:
    """Serializeaza optimizarea fara a prelua limita tranzactiei apelantului.

    Redis evita lucrul concurent obisnuit, iar lock-ul PostgreSQL transaction-scoped
    din `_run_locked` ramane activ pana la commit/rollback-ul facut de apelant.
    """
    lock = _acquire_lock(station_id)
    try:
        return _run_locked(db, station_id, triggered_by, triggered_by_user_id)
    finally:
        with contextlib.suppress(Exception):
            lock.release()


def _run_locked(db: Session, station_id: uuid.UUID, triggered_by: str, triggered_by_user_id) -> OptimizationRun:
    # Acest lock ramane activ pana la commit/rollback-ul tranzactiei apelantului,
    # astfel incat urmatoarea rulare vede obligatoriu versiunea deja publicata.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(station_id), 0))))
    station = db.get(Station, station_id)
    if station is None:
        raise ValueError("Statia nu exista.")

    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.station_id == station_id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )

    interval_minutes = settings.optimization_interval_minutes
    start = _round_to_interval(utcnow(), interval_minutes) + timedelta(minutes=interval_minutes)
    horizon_hours = settings.optimization_horizon_hours
    n_intervals = int(horizon_hours * 60 / interval_minutes)
    horizon = [start + timedelta(minutes=interval_minutes * i) for i in range(n_intervals)]
    end = horizon[-1] + timedelta(minutes=interval_minutes)

    run = OptimizationRun(
        station_id=station_id,
        status=OptimizationRunStatus.running.value,
        horizon_start=start,
        horizon_end=end,
        interval_minutes=interval_minutes,
        station_config_version_id=config.id if config else None,
        preference_version_id=preference.id if preference else None,
        solver_timeout_seconds=settings.optimization_solver_timeout_seconds,
        triggered_by=triggered_by,
        triggered_by_user_id=triggered_by_user_id,
        started_at=utcnow(),
    )
    db.add(run)
    db.flush()

    if config is None or preference is None:
        return _fallback(db, run, station, horizon, interval_minutes, "Statia nu are configuratie sau preferinte publicate.")

    _ensure_forecasts(db, station, start, end)

    pv_series_raw = _build_pv_series(db, station_id, horizon)
    load_series_raw = _build_load_series(db, station_id, horizon)

    if not any(v is not None for v in pv_series_raw.values()) and not any(v is not None for v in load_series_raw.values()):
        return _fallback(db, run, station, horizon, interval_minutes, "Prognoze PV si de consum indisponibile pentru orizontul cerut.")

    pv_series = _fill_gaps(pv_series_raw, fallback=0.0)
    load_series = _fill_gaps(load_series_raw, fallback=max([v for v in load_series_raw.values() if v], default=0.5))

    price_buy_raw, price_sell_raw = {}, {}
    for t in horizon:
        imp = tariff_service.get_current_tariff_version(db, station_id, "import", t)
        exp = tariff_service.get_current_tariff_version(db, station_id, "export", t)
        # Fixture-urile sintetice de piata (import demo/diagnostic) nu trebuie sa alimenteze
        # niciodata un pret folosit intr-un plan live -- le tratam ca inexistente aici; golul
        # ramas e completat mai jos explicit ca "estimated", niciodata ca pret real.
        market_row = db.execute(
            select(MarketPriceInterval, ImportRun.is_synthetic_fixture)
            .join(ImportRun, ImportRun.id == MarketPriceInterval.import_run_id)
            .where(
                MarketPriceInterval.is_current.is_(True),
                MarketPriceInterval.interval_start <= t,
                MarketPriceInterval.interval_end > t,
            )
        ).first()
        market = market_row[0] if market_row and not market_row[1] else None
        price_buy_raw[t] = _resolve_price(imp, market)
        price_sell_raw[t] = _resolve_price(exp, market)

    if all(v is None for v in price_buy_raw.values()):
        return _fallback(db, run, station, horizon, interval_minutes, "Niciun tarif de import valid pentru orizontul cerut.")

    price_buy, price_buy_quality = _fill_price_gaps(price_buy_raw)
    # Pretul de export lipsa ramane implicit 0 (nicio ipoteza de venit necunoscut), nu
    # imprumutat de la un alt interval -- comportament conservator neschimbat.
    price_sell = {t: (v if v is not None else 0.0) for t, v in price_sell_raw.items()}

    soc_kwh, soc_measured_at, soc_quality = _current_soc_kwh(db, station, config.battery_available_capacity_kwh or Decimal(0))
    if station.execution_mode == ExecutionMode.live.value and soc_quality != "measured":
        return _fallback(
            db, run, station, horizon, interval_minutes,
            f"SOC baterie {soc_quality} (prag prospetime {settings.optimization_soc_max_age_minutes} min) -- blocheaza planul live.",
        )

    try:
        result = _solve(
            station=station, config=config, preference=preference, horizon=horizon,
            interval_minutes=interval_minutes, pv_series=pv_series, load_series=load_series,
            price_buy=price_buy, price_sell=price_sell, current_soc_kwh=soc_kwh,
            db=db,
        )
    except Exception as exc:
        logger.error("optimization.solve_error", station_id=str(station_id), error=str(exc))
        return _fallback(db, run, station, horizon, interval_minutes, f"Eroare in timpul rezolvarii: {exc}")

    if result["termination"] not in ("optimal", "feasible"):
        reason = {
            "infeasible": "Modelul este infezabil cu constrangerile obligatorii curente (verifica SOC min/max, limite de putere si bugete EFC).",
            "timeout": f"Solverul a depasit limita de timp ({settings.optimization_solver_timeout_seconds}s).",
        }.get(result["termination"], f"Solver terminat cu status neasteptat: {result['termination']}")
        run.status = (
            OptimizationRunStatus.infeasible.value if result["termination"] == "infeasible" else OptimizationRunStatus.solver_timeout.value
        )
        return _fallback(db, run, station, horizon, interval_minutes, reason)

    price_estimated_count = sum(1 for q in price_buy_quality.values() if q == "estimated")

    run.status = OptimizationRunStatus.succeeded.value
    run.objective_value_lei = Decimal(str(round(result["objective"], 4)))
    run.finished_at = utcnow()
    run.input_snapshot = {
        "schema_version": 1,
        "horizon": {
            "start": start.isoformat(), "end": end.isoformat(),
            "interval_minutes": interval_minutes, "n_intervals": len(horizon),
            "station_timezone": station.timezone,
        },
        "soc": {
            "kwh": round(soc_kwh, 4), "quality": soc_quality,
            "measured_at": soc_measured_at.isoformat() if soc_measured_at else None,
            "max_age_minutes": settings.optimization_soc_max_age_minutes,
        },
        "station": {"timezone": station.timezone, "execution_mode": station.execution_mode},
        "config": {
            "id": str(config.id),
            "version": config.version,
            "battery_reference_capacity_kwh": str(config.battery_reference_capacity_kwh) if config.battery_reference_capacity_kwh is not None else None,
            "battery_available_capacity_kwh": str(config.battery_available_capacity_kwh) if config.battery_available_capacity_kwh is not None else None,
            "battery_max_charge_power_kw": str(config.battery_max_charge_power_kw) if config.battery_max_charge_power_kw is not None else None,
            "battery_max_discharge_power_kw": str(config.battery_max_discharge_power_kw) if config.battery_max_discharge_power_kw is not None else None,
            "battery_charge_efficiency": str(config.battery_charge_efficiency) if config.battery_charge_efficiency is not None else None,
            "battery_discharge_efficiency": str(config.battery_discharge_efficiency) if config.battery_discharge_efficiency is not None else None,
            "grid_import_limit_kw": str(config.grid_import_limit_kw) if config.grid_import_limit_kw is not None else None,
            "grid_export_limit_kw": str(config.grid_export_limit_kw) if config.grid_export_limit_kw is not None else None,
            "ev_enabled": config.ev_enabled,
            "ev_max_charge_power_kw": str(config.ev_max_charge_power_kw) if config.ev_max_charge_power_kw is not None else None,
        },
        "preference": {
            "id": str(preference.id),
            "version": preference.version,
            "min_reserve_soc_percent": str(preference.min_reserve_soc_percent),
            "max_normal_soc_percent": str(preference.max_normal_soc_percent),
            "allow_grid_charge": preference.allow_grid_charge,
            "allow_battery_export": preference.allow_battery_export,
            "max_efc_per_day": str(preference.max_efc_per_day) if preference.max_efc_per_day is not None else None,
            "max_efc_per_month": str(preference.max_efc_per_month) if preference.max_efc_per_month is not None else None,
            "priority": preference.priority,
            "soc_targets": preference.soc_targets,
            "ev_required_energy_kwh": str(preference.ev_required_energy_kwh) if preference.ev_required_energy_kwh is not None else None,
            "ev_departure_time": preference.ev_departure_time.isoformat() if preference.ev_departure_time else None,
        },
        "pv_forecast_raw_kw": {t.isoformat(): v for t, v in pv_series_raw.items()},
        "pv_forecast_kw": {t.isoformat(): v for t, v in pv_series.items()},
        "load_forecast_raw_kw": {t.isoformat(): v for t, v in load_series_raw.items()},
        "load_forecast_kw": {t.isoformat(): v for t, v in load_series.items()},
        "price_buy_lei_kwh": {t.isoformat(): v for t, v in price_buy.items()},
        "price_buy_quality": {t.isoformat(): q for t, q in price_buy_quality.items()},
        "price_sell_lei_kwh": {t.isoformat(): v for t, v in price_sell.items()},
        "pv_coverage": sum(1 for v in pv_series_raw.values() if v is not None),
        "load_coverage": sum(1 for v in load_series_raw.values() if v is not None),
        "price_estimated_count": price_estimated_count,
    }

    shadow_downgrade_reason = None
    if station.execution_mode == ExecutionMode.live.value and price_estimated_count > 0:
        shadow_downgrade_reason = (
            f"{price_estimated_count} din {len(horizon)} intervale de pret import sunt estimate "
            "(fara sursa reala/nesintetica) -- planul ramane shadow pana la date reale."
        )

    run.explanation_summary = (
        f"Cost net estimat pe orizont: {result['objective']:.2f} lei. "
        f"Prioritate: {preference.priority}. Interval optimizat: {start:%d.%m %H:%M} - {end:%d.%m %H:%M}."
        + (f" {shadow_downgrade_reason}" if shadow_downgrade_reason else "")
    )
    db.add(run)
    db.flush()

    _publish_plan(db, run, station, result["intervals"], price_buy, preference.priority, shadow_downgrade_reason=shadow_downgrade_reason)
    return run


def _resolve_price(tariff_version, market) -> float | None:
    if tariff_version is None or tariff_version.economic_calculation_disabled:
        return None
    if tariff_version.fixed_price_lei_per_kwh is not None:
        base = float(tariff_version.fixed_price_lei_per_kwh)
    elif tariff_version.opcom_margin_lei_per_kwh is not None and market is not None:
        base = float(market.price_lei_per_kwh) + float(tariff_version.opcom_margin_lei_per_kwh)
    else:
        return None
    return base + float(tariff_version.variable_component_lei_per_kwh)


def _solve(*, station, config, preference, horizon, interval_minutes, pv_series, load_series, price_buy, price_sell, current_soc_kwh, db):
    dt_h = interval_minutes / 60.0
    T = list(range(len(horizon)))

    ref_capacity = float(config.battery_reference_capacity_kwh or 0) or 1.0
    avail_capacity = float(config.battery_available_capacity_kwh or config.battery_reference_capacity_kwh or 1.0)
    max_charge_kw = float(config.battery_max_charge_power_kw) if config.battery_max_charge_power_kw is not None else avail_capacity
    max_discharge_kw = float(config.battery_max_discharge_power_kw) if config.battery_max_discharge_power_kw is not None else avail_capacity
    eff_c = float(config.battery_charge_efficiency or 0.95)
    eff_d = float(config.battery_discharge_efficiency or 0.95)
    import_limit = float(config.grid_import_limit_kw) if config.grid_import_limit_kw is not None else 1e6
    export_limit = float(config.grid_export_limit_kw) if config.grid_export_limit_kw is not None else 1e6

    soc_min = float(preference.min_reserve_soc_percent) / 100.0 * avail_capacity
    soc_max = float(preference.max_normal_soc_percent) / 100.0 * avail_capacity
    current_soc_kwh = min(max(current_soc_kwh, soc_min), soc_max)

    weights = PRIORITY_WEIGHTS.get(preference.priority, PRIORITY_WEIGHTS["cost"])

    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=T, ordered=True)
    big_m = max(import_limit, export_limit, max_charge_kw, max_discharge_kw) * 2 + 1

    m.grid_import = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0, import_limit))
    m.grid_export = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0, export_limit))
    m.batt_charge = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0, max_charge_kw))
    m.batt_discharge = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0, max_discharge_kw))
    m.soc = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(soc_min, soc_max))
    m.z_batt = pyo.Var(m.T, domain=pyo.Binary)
    m.z_grid = pyo.Var(m.T, domain=pyo.Binary)

    ev_enabled = bool(config.ev_enabled and preference.ev_required_energy_kwh)
    ev_max_kw = float(config.ev_max_charge_power_kw or 0) if ev_enabled else 0.0
    m.ev_charge = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0, ev_max_kw))

    m.soc_dev_pos = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.soc_dev_neg = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.ev_shortfall = pyo.Var(domain=pyo.NonNegativeReals)

    soc_targets_kwh = _resolve_soc_targets(preference, horizon, avail_capacity, station.timezone)

    def balance_rule(m, t):
        tm = horizon[t]
        return (
            pv_series[tm] + m.batt_discharge[t] + m.grid_import[t]
            == load_series[tm] + m.batt_charge[t] + m.grid_export[t] + m.ev_charge[t]
        )

    m.balance = pyo.Constraint(m.T, rule=balance_rule)

    def soc_dynamics_rule(m, t):
        prev = current_soc_kwh if t == 0 else m.soc[t - 1]
        return m.soc[t] == prev + (m.batt_charge[t] * eff_c - m.batt_discharge[t] / eff_d) * dt_h

    m.soc_dynamics = pyo.Constraint(m.T, rule=soc_dynamics_rule)

    m.no_simultaneous_batt_charge = pyo.Constraint(m.T, rule=lambda m, t: m.batt_charge[t] <= big_m * m.z_batt[t])
    m.no_simultaneous_batt_discharge = pyo.Constraint(m.T, rule=lambda m, t: m.batt_discharge[t] <= big_m * (1 - m.z_batt[t]))
    m.no_simultaneous_grid_import = pyo.Constraint(m.T, rule=lambda m, t: m.grid_import[t] <= big_m * m.z_grid[t])
    m.no_simultaneous_grid_export = pyo.Constraint(m.T, rule=lambda m, t: m.grid_export[t] <= big_m * (1 - m.z_grid[t]))

    if not preference.allow_grid_charge:
        m.no_grid_charge = pyo.Constraint(m.T, rule=lambda m, t: m.batt_charge[t] <= pv_series[horizon[t]])
    if not preference.allow_battery_export:
        m.no_battery_export = pyo.Constraint(
            m.T, rule=lambda m, t: m.batt_discharge[t] <= load_series[horizon[t]] + m.ev_charge[t]
        )

    def soc_target_rule(m, t):
        target = soc_targets_kwh.get(t)
        if target is None:
            return pyo.Constraint.Skip
        return m.soc[t] - target == m.soc_dev_pos[t] - m.soc_dev_neg[t]

    m.soc_target = pyo.Constraint(m.T, rule=soc_target_rule)

    if ev_enabled:
        departure_idx = _departure_index(preference, horizon, station.timezone)
        required = float(preference.ev_required_energy_kwh)
        relevant = [t for t in T if departure_idx is None or t < departure_idx]
        m.ev_energy_target = pyo.Constraint(
            expr=sum(m.ev_charge[t] * dt_h for t in relevant) + m.ev_shortfall >= required
        )

    efc_day_groups = _group_by_local_day(horizon, station.timezone)
    if preference.max_efc_per_day is not None:
        for day, idxs in efc_day_groups.items():
            m.add_component(
                f"efc_day_{day}",
                pyo.Constraint(expr=sum(m.batt_discharge[t] * dt_h for t in idxs) <= float(preference.max_efc_per_day) * ref_capacity),
            )
    if preference.max_efc_per_month is not None:
        already_used = get_efc_used(db, station, config, horizon[0].replace(day=1), horizon[0]) or 0.0
        remaining_budget = max(float(preference.max_efc_per_month) - already_used, 0.0)
        m.efc_month = pyo.Constraint(expr=sum(m.batt_discharge[t] * dt_h for t in T) <= remaining_budget * ref_capacity)

    terminal_value = price_buy[horizon[-1]] * weights["terminal_value"]

    objective_expr = (
        sum((m.grid_import[t] * price_buy[horizon[t]] - m.grid_export[t] * price_sell[horizon[t]]) * dt_h for t in T)
        + weights["wear"] * float(DEFAULT_BATTERY_WEAR_COST_LEI_PER_KWH) * sum((m.batt_charge[t] + m.batt_discharge[t]) * dt_h for t in T)
        + weights["soc_target"] * SOC_TARGET_PENALTY_LEI_PER_KWH * sum(m.soc_dev_pos[t] + m.soc_dev_neg[t] for t in T)
        + weights["ev"] * EV_SHORTFALL_PENALTY_LEI_PER_KWH * m.ev_shortfall
        - terminal_value * m.soc[T[-1]]
    )
    m.objective = pyo.Objective(expr=objective_expr, sense=pyo.minimize)

    solver = pyo.SolverFactory("appsi_highs")
    solver.options["time_limit"] = settings.optimization_solver_timeout_seconds
    solver_result = solver.solve(m, load_solutions=False)

    tc = solver_result.solver.termination_condition
    if tc == TerminationCondition.optimal or tc == TerminationCondition.feasible:
        termination = "optimal"
    elif tc == TerminationCondition.infeasible:
        termination = "infeasible"
    elif tc in (TerminationCondition.maxTimeLimit,):
        termination = "timeout"
    else:
        termination = str(tc)

    if termination not in ("optimal",):
        return {"termination": termination, "objective": None, "intervals": []}

    m.solutions.load_from(solver_result)

    intervals = []
    for t in T:
        intervals.append(
            {
                "interval_start": horizon[t],
                "interval_end": horizon[t] + timedelta(minutes=interval_minutes),
                "pv_forecast_kw": pv_series[horizon[t]],
                "load_forecast_kw": load_series[horizon[t]],
                "battery_power_target_kw": pyo.value(m.batt_charge[t]) - pyo.value(m.batt_discharge[t]),
                "grid_power_target_kw": pyo.value(m.grid_import[t]) - pyo.value(m.grid_export[t]),
                "battery_soc_target_percent": pyo.value(m.soc[t]) / avail_capacity * 100.0 if avail_capacity else 0.0,
                "ev_charge_power_kw": pyo.value(m.ev_charge[t]),
                "price_import_lei_kwh": price_buy[horizon[t]],
                "price_export_lei_kwh": price_sell[horizon[t]],
            }
        )

    return {"termination": "optimal", "objective": pyo.value(m.objective), "intervals": intervals}


def _resolve_soc_targets(preference, horizon, avail_capacity, tz_name) -> dict[int, float]:
    tz = ZoneInfo(tz_name)
    out = {}
    for idx, t in enumerate(horizon):
        local = t.astimezone(tz)
        for target in preference.soc_targets or []:
            try:
                hh, mm = target["time"].split(":")
                days = target.get("days_of_week")
                if days is not None and local.weekday() not in days:
                    continue
                if local.hour == int(hh) and local.minute == int(mm):
                    out[idx] = float(target["target_soc_percent"]) / 100.0 * avail_capacity
            except (KeyError, ValueError):
                continue
    return out


def _departure_index(preference, horizon, tz_name) -> int | None:
    if not preference.ev_departure_time:
        return None
    tz = ZoneInfo(tz_name)
    for idx, t in enumerate(horizon):
        local = t.astimezone(tz)
        if local.hour == preference.ev_departure_time.hour and local.minute >= preference.ev_departure_time.minute:
            return idx
    return None


def _group_by_local_day(horizon, tz_name) -> dict[str, list[int]]:
    tz = ZoneInfo(tz_name)
    groups: dict[str, list[int]] = {}
    for idx, t in enumerate(horizon):
        key = t.astimezone(tz).date().isoformat()
        groups.setdefault(key, []).append(idx)
    return groups


def _fallback(db: Session, run: OptimizationRun, station: Station, horizon: list[datetime], interval_minutes: int, reason: str) -> OptimizationRun:
    run.status = run.status if run.status in (
        OptimizationRunStatus.infeasible.value, OptimizationRunStatus.solver_timeout.value
    ) else OptimizationRunStatus.fallback.value
    run.is_fallback = True
    run.fallback_reason = reason
    run.finished_at = utcnow()
    run.explanation_summary = f"Plan de asteptare (fallback conservator): {reason}"
    db.add(run)
    db.flush()

    intervals = [
        {
            "interval_start": t,
            "interval_end": t + timedelta(minutes=interval_minutes),
            "pv_forecast_kw": 0,
            "load_forecast_kw": 0,
            "battery_power_target_kw": 0,
            "grid_power_target_kw": 0,
            "battery_soc_target_percent": 0,
            "ev_charge_power_kw": 0,
            "price_import_lei_kwh": None,
            "price_export_lei_kwh": None,
        }
        for t in horizon
    ]
    _publish_plan(db, run, station, intervals, {}, "cost", fallback_reason=reason)
    return run


def _publish_plan(
    db: Session, run: OptimizationRun, station: Station, intervals: list[dict], price_buy: dict, priority: str,
    fallback_reason: str | None = None, shadow_downgrade_reason: str | None = None,
) -> Plan:
    # Versiunea e monotona pe TOATE planurile statiei vreodata create, indiferent de
    # status -- un plan `completed` (executie terminata) nu mai apare in filtrul de mai
    # jos (care cauta doar planul activ de inlocuit), dar trebuie sa ramana socotit la
    # numerotare, altfel versiunea reincepe gresit de la 1 dupa ce planurile active se
    # inchid.
    max_version = db.scalar(select(func.max(Plan.version)).where(Plan.station_id == station.id))
    next_version = (max_version or 0) + 1

    previous = db.scalar(
        select(Plan)
        .where(Plan.station_id == station.id, Plan.status.in_([PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value]))
        .order_by(Plan.version.desc())
        .limit(1)
    )
    if previous is not None:
        previous.status = PlanStatus.superseded.value
        previous.superseded_at = utcnow()
        db.add(previous)

    plan = Plan(
        optimization_run_id=run.id,
        station_id=station.id,
        version=next_version,
        status=PlanStatus.published.value,
        # Fallback intervals are diagnostic placeholders, never physical setpoints; a plan
        # built from estimated/synthetic-derived prices also stays shadow-only until real data exists.
        execution_mode="shadow" if (fallback_reason is not None or shadow_downgrade_reason is not None) else station.execution_mode,
        published_at=utcnow(),
    )
    db.add(plan)
    db.flush()

    if previous is not None:
        previous.superseded_by_plan_id = plan.id
        db.add(previous)

    for data in intervals:
        explanation = (
            f"Plan de asteptare: {fallback_reason}" if fallback_reason else _explain_interval(data, priority)
        )
        db.add(
            PlanInterval(
                plan_id=plan.id,
                interval_start=data["interval_start"],
                interval_end=data["interval_end"],
                pv_forecast_kw=Decimal(str(round(data["pv_forecast_kw"], 4))),
                load_forecast_kw=Decimal(str(round(data["load_forecast_kw"], 4))),
                battery_power_target_kw=Decimal(str(round(data["battery_power_target_kw"], 4))),
                grid_power_target_kw=Decimal(str(round(data["grid_power_target_kw"], 4))),
                battery_soc_target_percent=Decimal(str(round(data["battery_soc_target_percent"], 2))),
                ev_charge_power_kw=Decimal(str(round(data["ev_charge_power_kw"], 4))),
                price_import_lei_kwh=Decimal(str(round(data["price_import_lei_kwh"], 6))) if data["price_import_lei_kwh"] is not None else None,
                price_export_lei_kwh=Decimal(str(round(data["price_export_lei_kwh"], 6))) if data["price_export_lei_kwh"] is not None else None,
                explanation=explanation,
            )
        )
    db.flush()
    return plan
