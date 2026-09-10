from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.device import Device
from app.models.enums import PlanStatus
from app.models.market import MarketPriceInterval
from app.models.optimization import Plan, PlanInterval
from app.models.station import Station, StationConfigVersion
from app.models.tariff import TariffVersion
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


def _w_to_kw(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value) / 1000.0


def _effective_price(tariff_version: TariffVersion | None, market_price: MarketPriceInterval | None) -> float | None:
    if tariff_version is None or tariff_version.economic_calculation_disabled:
        return None
    if tariff_version.fixed_price_lei_per_kwh is not None:
        base = float(tariff_version.fixed_price_lei_per_kwh)
    elif tariff_version.opcom_margin_lei_per_kwh is not None and market_price is not None:
        base = float(market_price.price_lei_per_kwh) + float(tariff_version.opcom_margin_lei_per_kwh)
    else:
        return None
    return round(base + float(tariff_version.variable_component_lei_per_kwh), 6)


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
            "pv_kwh": float(r.pv_energy_kwh),
            "load_kwh": float(r.load_energy_kwh),
            "grid_import_kwh": float(r.grid_import_energy_kwh),
            "grid_export_kwh": float(r.grid_export_energy_kwh),
            "battery_charge_kwh": float(r.battery_charge_energy_kwh),
            "battery_discharge_kwh": float(r.battery_discharge_energy_kwh),
            "data_quality": r.data_quality,
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
        forecasts = db.scalars(
            select(PvForecast).where(
                PvForecast.station_id == station.id,
                PvForecast.scenario == "expected",
                PvForecast.interval_start >= start,
                PvForecast.interval_start < end,
            )
        ).all()
    else:
        forecasts = db.scalars(
            select(ConsumptionForecast).where(
                ConsumptionForecast.station_id == station.id,
                ConsumptionForecast.interval_start >= start,
                ConsumptionForecast.interval_start < end,
            )
        ).all()

    out = []
    for f in forecasts:
        actual = actual_by_start.get(f.interval_start)
        actual_kw = None
        if actual is not None:
            energy = actual.pv_energy_kwh if metric == "pv" else actual.load_energy_kwh
            actual_kw = float(energy) * 4  # kWh pe interval de 15 min -> kW mediu
        forecast_kw = float(f.predicted_power_kw) if metric == "pv" else float(f.base_load_kw + f.ev_component_kw + f.flexible_component_kw)
        out.append({"t": f.interval_start.isoformat(), "forecast_kw": forecast_kw, "actual_kw": actual_kw, "is_synthetic": f.is_synthetic})
    return out


def get_estimated_savings(db: Session, station: Station, start: datetime, end: datetime) -> dict:
    """Economie estimata fata de un reper explicit: costul ipotetic daca TOT
    consumul ar fi fost importat din retea la tariful de import curent, fara
    PV/baterie. Reperul e documentat explicit in UI -- nu e o valoare 'magica'."""
    now = utcnow()
    import_tariff = tariff_service.get_current_tariff_version(db, station.id, "import", now)
    if import_tariff is None or import_tariff.economic_calculation_disabled or import_tariff.fixed_price_lei_per_kwh is None:
        return {"available": False, "reason": "Tarif de import fix indisponibil sau calcul economic dezactivat."}

    price = float(import_tariff.fixed_price_lei_per_kwh)
    totals = db.execute(
        select(
            func.coalesce(func.sum(TelemetryAggregate.load_energy_kwh), 0),
            func.coalesce(func.sum(TelemetryAggregate.grid_import_energy_kwh), 0),
        ).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "hour",
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
    ).one()
    total_load_kwh, total_import_kwh = float(totals[0]), float(totals[1])
    baseline_cost = total_load_kwh * price
    actual_cost = total_import_kwh * price
    return {
        "available": True,
        "baseline_description": "Cost ipotetic daca tot consumul era importat din retea, fara PV/baterie.",
        "baseline_cost_lei": round(baseline_cost, 2),
        "actual_import_cost_lei": round(actual_cost, 2),
        "estimated_savings_lei": round(baseline_cost - actual_cost, 2),
        "total_load_kwh": round(total_load_kwh, 3),
    }
