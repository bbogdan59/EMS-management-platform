from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import utcnow
from app.models.forecast import ConsumptionForecast, PvForecast, WeatherForecast
from app.models.preference import PreferenceVersion
from app.models.station import StationConfigVersion
from app.services import battery_projection_service as service
from app.services.battery_service import midnight
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.integration.test_battery_health import sample
from tests.web_helpers import login


@pytest.fixture()
def context(db):
    user = make_user(db, email=f"projection-{uuid4()}@test.local")
    org = make_org(db)
    make_membership(db, user, org, "viewer")
    station = make_station(db, org, user)
    device = make_device(db, station)
    config = db.scalar(
        select(StationConfigVersion).where(StationConfigVersion.station_id == station.id)
    )
    config.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    config.battery_charge_efficiency = config.battery_discharge_efficiency = Decimal(1)
    config.battery_max_charge_power_kw = config.battery_max_discharge_power_kw = Decimal(10)
    pref = db.scalar(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id))
    pref.created_at = config.created_at
    pref.max_normal_soc_percent = Decimal(100)
    pref.min_reserve_soc_percent = Decimal(10)
    now = datetime(2026, 9, 30, 8, tzinfo=UTC)
    row = sample(db, station, device, now, power=2000, soc=50, load_power_w=1000)
    db.flush()
    return user, station, device, config, pref, row, now


def forecasts(db, station, now, pv=3, load=1):
    end = service.calculate(db, station, now=now).horizon_end
    rows = []
    start = now.replace(minute=0, second=0, microsecond=0)
    while start < end:
        stop = min(start + timedelta(hours=1), end)
        weather = WeatherForecast(
            station_id=station.id, issued_at=now, interval_start=start, interval_end=stop
        )
        db.add(weather)
        db.flush()
        solar = PvForecast(
            station_id=station.id,
            issued_at=now,
            interval_start=start,
            interval_end=stop,
            predicted_power_kw=Decimal(pv),
            based_on_weather_forecast_id=weather.id,
        )
        db.add(solar)
        consumption = None
        if load is not None:
            consumption = ConsumptionForecast(
                station_id=station.id,
                issued_at=now,
                interval_start=start,
                interval_end=stop,
                base_load_kw=Decimal(load),
            )
            db.add(consumption)
        rows.append((solar, weather, consumption))
        start = stop
    db.flush()
    return rows


def test_constant_rate_and_solar_eta_use_decimal_capacity_and_actual_dc_power(db, context):
    _, station, _, _, _, row, now = context
    forecasts(db, station, now)
    result = service.calculate(db, station, now=now)
    assert result.energy_to_target_kwh == Decimal(5)
    assert result.current_rate.reaches_target_at == now + timedelta(hours=2, minutes=30)
    assert result.solar.reaches_target_at == result.current_rate.reaches_target_at
    assert result.solar.end_soc_percent == 100
    assert result.power.value == 2 and result.power.received_at == row.received_at
    assert result.points[1].soc_percent == 55
    assert result.quality == "estimated" and result.confidence == "nominal"


def test_power_anchor_and_changing_sun_affect_curve_without_assuming_constant_solar(db, context):
    _, station, _, _, _, row, now = context
    row.battery_power_w = 0
    rows = forecasts(db, station, now, pv=5)
    for solar, _, _ in rows[1:]:
        solar.predicted_power_kw = 0
    db.flush()
    result = service.calculate(db, station, now=now)
    assert result.current_rate.status == "not_charging"
    assert result.points[1].soc_percent == 55  # mean of 0 kW observed and 4 kW surplus
    assert result.points[4].soc_percent == 85
    assert result.solar.status == "not_reached" and result.solar.reaches_target_at is None
    assert result.solar.peak_soc_percent == 85 and result.solar.end_soc_percent == 10


def test_efficiency_losses_limits_and_configured_soc_target(db, context):
    _, station, _, config, pref, _, now = context
    config.battery_charge_efficiency = Decimal("0.8")
    config.battery_max_charge_power_kw = Decimal(1)
    pref.max_normal_soc_percent = Decimal(80)
    db.flush()
    forecasts(db, station, now, pv=10)
    result = service.calculate(db, station, now=now)
    assert result.target_soc_percent == 80 and result.energy_to_target_kwh == 3
    assert (
        result.current_rate.minutes_to_target == 90
    )  # observed DC power: no second efficiency loss
    assert result.solar.minutes_to_target == 225
    assert result.solar.end_soc_percent == 80
    assert max(p.battery_power_kw for p in result.points[1:]) == Decimal("0.8")


def test_explicit_zero_soc_and_charge_limit_are_not_defaulted(db, context):
    _, station, _, config, pref, row, now = context
    row.battery_soc_percent = 0
    config.battery_max_charge_power_kw = 0
    db.flush()
    forecasts(db, station, now, pv=10)
    result = service.calculate(db, station, now=now)
    assert result.energy_to_target_kwh == 10
    assert result.solar.end_soc_percent == 0 and result.solar.reaches_target_at is None
    assert result.current_rate.minutes_to_target == 300
    pref.min_reserve_soc_percent = pref.max_normal_soc_percent = 0
    db.flush()
    assert service.calculate(db, station, now=now).current_rate.status == "already_at_target"


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("soc", "soc_unavailable"),
        ("stale", "soc_unavailable"),
        ("simulated", "soc_unavailable"),
        ("capacity_zero", "capacity_unavailable"),
        ("capacity_missing", "capacity_unavailable"),
        ("fault", "battery_fault"),
    ],
)
def test_invalid_initial_state_never_creates_an_eta(db, context, failure, reason):
    _, station, _, config, _, row, now = context
    if failure == "soc":
        row.battery_soc_percent = None
    elif failure == "stale":
        row.measured_at = now - timedelta(minutes=11)
    elif failure == "simulated":
        row.is_simulated = True
    elif failure == "capacity_zero":
        config.battery_available_capacity_kwh = 0
    elif failure == "capacity_missing":
        config.battery_available_capacity_kwh = config.battery_reference_capacity_kwh = None
    else:
        row.diagnostics = {"battery": {"state": "fault"}}
    db.flush()
    result = service.calculate(db, station, now=now)
    assert result.current_rate.reaches_target_at is None and result.solar.reaches_target_at is None
    assert result.solar.reason == reason and result.points == []


def test_measured_zero_load_fallback_is_labelled_and_missing_load_stays_unknown(db, context):
    _, station, _, _, _, row, now = context
    row.load_power_w = 0
    forecasts(db, station, now, pv=3, load=None)
    result = service.calculate(db, station, now=now)
    assert result.solar.status == "estimated"
    assert "constant_current_load" in result.flags and result.confidence == "low"
    row.load_power_w = None
    db.flush()
    result = service.calculate(db, station, now=now)
    assert result.solar.status == "unavailable" and result.solar.reason == "load_forecast_missing"
    assert result.current_rate.status == "estimated"


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("solar", "synthetic_forecast"),
        ("weather", "synthetic_forecast"),
        ("weather_stale", "stale_forecast"),
        ("weather_missing", "weather_forecast_missing"),
        ("load", "synthetic_forecast"),
        ("load_untrusted", "untrusted_forecast"),
        ("overlap", "solar_forecast_missing"),
    ],
)
def test_forecast_carry_in_quality_and_overlaps_block_solar_eta(db, context, failure, reason):
    _, station, _, _, _, row, now = context
    now += timedelta(minutes=5)
    row.measured_at = now
    solar, weather, load = forecasts(db, station, now)[0]
    if failure == "solar":
        solar.is_synthetic = True
    elif failure == "weather":
        weather.is_synthetic = True
    elif failure == "weather_stale":
        weather.issued_at = now - timedelta(hours=7)
    elif failure == "weather_missing":
        solar.based_on_weather_forecast_id = None
    elif failure == "load":
        load.is_synthetic = True
    elif failure == "load_untrusted":
        load.source_version = "weekday_v1_untrusted"
    else:
        db.add(
            PvForecast(
                station_id=station.id,
                issued_at=now,
                interval_start=now,
                interval_end=now + timedelta(minutes=10),
                predicted_power_kw=100,
            )
        )
    db.flush()
    result = service.calculate(db, station, now=now)
    assert result.solar.status == "unavailable" and result.solar.reason == reason
    assert result.current_rate.status == "estimated"
    assert reason in result.flags


def test_gap_stops_soc_propagation_and_new_generation_never_uses_old_fillers(db, context):
    _, station, _, _, _, _, now = context
    rows = forecasts(db, station, now)
    db.delete(rows[1][0])
    db.flush()
    result = service.calculate(db, station, now=now)
    assert result.solar.status == "partial" and result.solar.end_soc_percent is None
    assert result.solar.reaches_target_at is None
    assert (
        result.points[-1].at == now + timedelta(hours=1) and result.points[-1].soc_percent is None
    )
    db.add(
        PvForecast(
            station_id=station.id,
            issued_at=now + timedelta(seconds=1),
            interval_start=now + timedelta(hours=2),
            interval_end=now + timedelta(hours=3),
            predicted_power_kw=100,
        )
    )
    db.flush()
    assert (
        service.calculate(db, station, now=now + timedelta(minutes=1)).solar.status == "unavailable"
    )


def test_multiple_banks_never_receive_entire_station_solar_forecast(db, context):
    _, station, device, _, _, _, now = context
    other = make_device(db, station, "Second inverter")
    sample(
        db,
        station,
        other,
        now,
        power=1000,
        soc=40,
        diagnostics={"battery": {"nominal_capacity_kwh": "5"}},
    )
    forecasts(db, station, now)
    result = service.calculate(db, station, f"{other.id}:bank", now=now)
    assert result.current_rate.minutes_to_target == 180
    assert result.solar.reason == "ambiguous_battery_allocation"
    with pytest.raises(LookupError):
        service.calculate(db, station, f"{uuid4()}:bank", now=now)


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2024, 2, 29), 24)]
)
def test_projection_follows_local_dst_and_leap_day(db, context, day, hours):
    _, station, _, _, _, row, _ = context
    now = midnight(day, station.timezone)
    row.measured_at = row.received_at = now
    db.flush()
    forecasts(db, station, now, pv=0, load=0)
    result = service.calculate(db, station, now=now)
    assert result.horizon_end - now == timedelta(hours=hours)
    assert result.points[-1].at == result.horizon_end
    assert len(result.points) == hours * 4 + 1


def test_route_scope_no_store_and_decimal_contract(db, client, context):
    user, station, device, _, _, row, _ = context
    now = utcnow()
    row.measured_at = row.received_at = now
    db.commit()
    login(client, user.email, "TestPass1234")
    path = f"/api/v1/stations/{station.id}/battery-health/projection"
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["current_rate"]["status"] == "estimated"
    assert response.json()["power"]["value"] == "2.00"
    assert client.get(path, params={"battery_id": f"{uuid4()}:bank"}).status_code == 404
    other = make_station(db, make_org(db, "Other projection tenant"), user)
    db.commit()
    assert client.get(f"/api/v1/stations/{other.id}/battery-health/projection").status_code == 403
