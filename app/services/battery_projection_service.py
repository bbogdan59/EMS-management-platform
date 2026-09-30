"""Read-only conditional charge estimates; never a dispatch plan or hardware ACK."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.forecast import ConsumptionForecast, PvForecast, WeatherForecast
from app.models.preference import PreferenceVersion
from app.schemas.battery_projection import (
    BatteryChargeProjection,
    ChargeEstimate,
    ChargeProjectionPoint,
)
from app.services import battery_service as battery

ZERO = Decimal(0)
MAX_AGE = timedelta(hours=6)
BLEND_SECONDS = Decimal(900)


def duration(seconds):
    return timedelta(seconds=int(seconds.to_integral_value(rounding=ROUND_CEILING)))


def usable_metric(metric):
    return (
        metric.value is not None
        and metric.quality not in ("stale", "simulated", "missing")
        and not {"stale", "simulated"}.intersection(metric.flags)
    )


def latest_batch(db, model, station_id, now, end):
    # Choose the latest issue before validating it; older runs must not silently
    # fill gaps or hide a newer synthetic/stale generation.
    issued = db.scalar(
        select(model.issued_at)
        .where(
            model.station_id == station_id,
            model.issued_at <= now,
        )
        .order_by(model.issued_at.desc())
        .limit(1)
    )
    if issued is None:
        return None, []
    query = select(model).where(
        model.station_id == station_id,
        model.issued_at == issued,
        model.interval_start < end,
        model.interval_end > now,
    )
    if model is PvForecast:
        query = query.where(model.scenario == "expected")
    return issued, db.scalars(query.order_by(model.interval_start)).all()


def problem(row, now):
    if row.is_synthetic:
        return "synthetic_forecast"
    if not now - MAX_AGE <= row.issued_at <= now:
        return "stale_forecast"
    if "untrusted" in (row.source_version or ""):
        return "untrusted_forecast"
    if row.interval_end <= row.interval_start:
        return "invalid_forecast"
    return None


def at_interval(rows, start, end):
    covering = [r for r in rows if r.interval_start < end and r.interval_end > start]
    if len(covering) != 1:
        return None
    row = covering[0]
    return row if row.interval_start <= start and row.interval_end >= end else None


def calculate(db, station, battery_id=None, now=None):
    now = now or datetime.now(UTC)
    day = now.astimezone(ZoneInfo(station.timezone)).date()
    end = battery.midnight(day + timedelta(days=1), station.timezone)
    result = BatteryChargeProjection(
        station_id=station.id, timezone=station.timezone, generated_at=now, horizon_end=end
    )
    targets, rows, configs = battery.discover(db, station, now)
    target = (
        next((t for t in targets if t.id == battery_id), None)
        if battery_id
        else next(iter(targets), None)
    )
    if battery_id and target is None:
        raise LookupError("Bateria nu apartine surselor acestei statii.")
    if target is None:
        result.current_rate.reason = result.solar.reason = "no_battery"
        return result
    unambiguous = len(targets) == 1 and target.scope == "reported_bank"
    row = rows.get(target.device_id)
    live = battery.current(target, row, configs, unambiguous, now)
    result.target, result.soc, result.power = target, live.soc, live.power
    result.capacity = (
        live.usable_capacity if live.usable_capacity.value is not None else live.nominal_capacity
    )
    flags = set(live.soc.flags + live.power.flags + result.capacity.flags)
    if live.usable_capacity.value is None:
        flags.add("nominal_capacity_fallback")
    config = battery.config_at(configs, now) if unambiguous else None
    pref = (
        db.scalar(
            select(PreferenceVersion)
            .where(
                PreferenceVersion.station_id == station.id,
                PreferenceVersion.created_at <= now,
            )
            .order_by(PreferenceVersion.version.desc())
            .limit(1)
        )
        if unambiguous
        else None
    )
    if pref:
        result.target_soc_percent = pref.max_normal_soc_percent
        result.reserve_soc_percent = pref.min_reserve_soc_percent
        result.preference_version = pref.version
    result.config_version = config.version if config else None

    def blocked(reason):
        result.current_rate.reason = result.solar.reason = reason
        result.flags = sorted(flags)
        return result

    if not usable_metric(live.soc) or not ZERO <= live.soc.value <= 100:
        return blocked("soc_unavailable")
    if not usable_metric(result.capacity) or result.capacity.value <= 0:
        return blocked("capacity_unavailable")
    if live.state == "fault":
        return blocked("battery_fault")
    if (
        any(
            battery.decimal(v) is None
            for v in (result.reserve_soc_percent, result.target_soc_percent)
        )
        or not ZERO <= result.reserve_soc_percent <= result.target_soc_percent <= 100
    ):
        return blocked("invalid_limits")
    capacity = result.capacity.value
    energy = capacity * live.soc.value / 100
    ceiling = capacity * result.target_soc_percent / 100
    floor = capacity * result.reserve_soc_percent / 100
    result.energy_to_target_kwh = max(ZERO, ceiling - energy)
    result.points = [
        ChargeProjectionPoint(
            at=now, soc_percent=live.soc.value, battery_power_kw=live.power.value, kind="observed"
        )
    ]
    if energy >= ceiling:
        result.current_rate = ChargeEstimate(
            status="already_at_target", reaches_target_at=now, minutes_to_target=ZERO
        )
    elif not usable_metric(live.power):
        result.current_rate.reason = "power_unavailable"
    elif live.power.value <= 0:
        result.current_rate = ChargeEstimate(status="not_charging", reason="not_charging")
    else:
        hours = result.energy_to_target_kwh / live.power.value
        result.current_rate = (
            ChargeEstimate(status="beyond_horizon", reason="over_48_hours")
            if hours > 48
            else ChargeEstimate(
                status="estimated",
                reaches_target_at=now + duration(hours * 3600),
                minutes_to_target=hours * 60,
            )
        )
    if result.current_rate.status in ("estimated", "already_at_target", "beyond_horizon"):
        result.quality, result.confidence = "estimated", "low"

    reason = None
    if not unambiguous:
        reason = "ambiguous_battery_allocation"
    elif not usable_metric(live.power):
        reason = "power_unavailable"
    elif config is None or any(
        battery.decimal(v) is None
        for v in (
            config.battery_max_charge_power_kw,
            config.battery_max_discharge_power_kw,
            config.battery_charge_efficiency,
            config.battery_discharge_efficiency,
        )
    ):
        reason = "limits_unavailable"
    elif (
        config.battery_max_charge_power_kw < 0
        or config.battery_max_discharge_power_kw < 0
        or not ZERO < config.battery_charge_efficiency <= 1
        or not ZERO < config.battery_discharge_efficiency <= 1
    ):
        reason = "invalid_limits"
    if reason:
        result.solar.reason, result.flags = reason, sorted(flags)
        return result

    issued, solar = latest_batch(db, PvForecast, station.id, now, end)
    load_issued, loads = latest_batch(db, ConsumptionForecast, station.id, now, end)
    result.forecast_issued_at, result.consumption_issued_at = issued, load_issued
    constant_load = None
    if load_issued is None:
        load_quality, load_flags = battery.provenance(row, {}, now=now, core=True)
        load = battery.decimal(row.load_power_w)
        flags.update(load_flags)
        if load is not None and load >= 0 and load_quality not in ("stale", "simulated"):
            constant_load = load / 1000
            flags.add("constant_current_load")
    weather = {
        w.id: w
        for w in db.scalars(
            select(WeatherForecast).where(
                WeatherForecast.station_id == station.id,
                WeatherForecast.id.in_(
                    [
                        r.based_on_weather_forecast_id
                        for r in solar
                        if r.based_on_weather_forecast_id
                    ]
                ),
            )
        )
    }
    boundaries = {now, end}
    cursor = now
    while cursor < end:
        boundaries.add(cursor)
        cursor += timedelta(minutes=15)
    for forecast in [*solar, *loads]:
        boundaries.update(
            t for t in (forecast.interval_start, forecast.interval_end) if now < t < end
        )
    ordered = sorted(boundaries)
    reached = now if energy >= ceiling else None
    peak = live.soc.value
    complete = True
    for left, right in pairwise(ordered):
        pv = at_interval(solar, left, right)
        load = at_interval(loads, left, right)
        reason = "solar_forecast_missing" if pv is None else problem(pv, now)
        w = weather.get(pv.based_on_weather_forecast_id) if pv else None
        if reason is None:
            reason = "weather_forecast_missing" if w is None else problem(w, now)
        if reason is None and (
            w.interval_start > pv.interval_start or w.interval_end < pv.interval_end
        ):
            reason = "weather_forecast_missing"
        if reason is None and constant_load is None:
            reason = "load_forecast_missing" if load is None else problem(load, now)
        pv_kw = battery.decimal(pv.predicted_power_kw) if pv else None
        load_kw = (
            constant_load
            if constant_load is not None
            else (
                sum(
                    (
                        battery.decimal(v)
                        for v in (
                            load.base_load_kw,
                            load.ev_component_kw,
                            load.flexible_component_kw,
                        )
                    ),
                    ZERO,
                )
                if load
                and all(
                    battery.decimal(v) is not None and battery.decimal(v) >= 0
                    for v in (load.base_load_kw, load.ev_component_kw, load.flexible_component_kw)
                )
                else None
            )
        )
        if reason is None and (pv_kw is None or pv_kw < 0 or load_kw is None):
            reason = "invalid_forecast"
        if reason:
            flags.add(reason)
            result.solar.reason = reason
            result.points.append(ChargeProjectionPoint(at=left, soc_percent=None, kind="missing"))
            complete = False
            break
        if any(r.confidence == "low" for r in (pv, w, load) if r is not None):
            flags.add("low_confidence_forecast")
        if load and load.is_cold_start:
            flags.add("cold_start_load")
        surplus = pv_kw - load_kw
        charge_limit = config.battery_max_charge_power_kw * config.battery_charge_efficiency
        discharge_limit = (
            config.battery_max_discharge_power_kw / config.battery_discharge_efficiency
        )
        forecast_power = (
            min(surplus, config.battery_max_charge_power_kw) * config.battery_charge_efficiency
            if surplus >= 0
            else max(surplus, -config.battery_max_discharge_power_kw)
            / config.battery_discharge_efficiency
        )
        elapsed = (battery.seconds(left - now) + battery.seconds(right - now)) / 2
        weight = max(ZERO, 1 - elapsed / BLEND_SECONDS)
        power = max(
            -discharge_limit,
            min(charge_limit, weight * live.power.value + (1 - weight) * forecast_power),
        )
        hours = battery.seconds(right - left) / 3600
        # An initial SOC outside preferences is observed, not clamped away.
        if power > 0:
            gain = min(power * hours, max(ZERO, ceiling - energy))
            if reached is None and energy < ceiling <= energy + gain:
                reached = left + duration((ceiling - energy) / power * 3600)
            energy += gain
        else:
            energy -= min(-power * hours, max(ZERO, energy - floor))
        soc = energy / capacity * 100
        peak = max(peak, soc)
        result.points.append(
            ChargeProjectionPoint(
                at=right,
                soc_percent=soc,
                pv_kw=pv_kw,
                load_kw=load_kw,
                battery_power_kw=power,
                kind="estimated",
            )
        )
    projected = any(p.kind == "estimated" for p in result.points)
    result.solar = ChargeEstimate(
        status=(
            "already_at_target" if reached == now else "estimated" if reached else "not_reached"
        )
        if complete
        else "partial"
        if projected
        else "unavailable",
        reaches_target_at=reached if projected else None,
        minutes_to_target=battery.seconds(reached - now) / 60 if reached and projected else None,
        end_soc_percent=result.points[-1].soc_percent if complete else None,
        peak_soc_percent=peak if projected else None,
        reason=result.solar.reason,
    )
    if projected:
        result.quality = "estimated"
        result.confidence = "low" if flags or not complete else "nominal"
    result.flags = sorted(flags)
    return result
