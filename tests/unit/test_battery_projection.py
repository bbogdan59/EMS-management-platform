from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.schemas.battery import BatteryTarget
from app.services import battery_projection_service as service


@pytest.mark.parametrize(
    "power,pv,expected_first_soc,rate_status",
    [
        ("2", "3", "55", "estimated"),
        ("0", "5", "55", "not_charging"),
        ("-2", "3", "50", "not_charging"),
    ],
)
def test_short_term_observation_blends_into_solar_surplus(
    monkeypatch, power, pv, expected_first_soc, rate_status
):
    now = datetime(2026, 9, 30, 8, tzinfo=UTC)
    station = SimpleNamespace(id=uuid4(), timezone="Europe/Bucharest")
    target = BatteryTarget(
        id="test:bank",
        device_id=uuid4(),
        label="Bank",
        source="device_rs485",
        scope="reported_bank",
    )
    observation = SimpleNamespace(
        diagnostics={},
        quality_flags={},
        is_simulated=False,
        is_late=False,
        measured_at=now,
        received_at=now,
        source="device_rs485",
        battery_power_w=Decimal(power) * 1000,
        battery_soc_percent=Decimal(50),
        load_power_w=1000,
    )
    config = SimpleNamespace(
        created_at=now - timedelta(days=1),
        version=1,
        battery_available_capacity_kwh=Decimal(10),
        battery_reference_capacity_kwh=Decimal(10),
        battery_max_charge_power_kw=Decimal(10),
        battery_max_discharge_power_kw=Decimal(10),
        battery_charge_efficiency=Decimal(1),
        battery_discharge_efficiency=Decimal(1),
    )
    monkeypatch.setattr(
        service.battery,
        "discover",
        lambda *args: ([target], {target.device_id: observation}, [config]),
    )
    end = datetime(2026, 9, 30, 21, tzinfo=UTC)
    common = {"issued_at": now, "interval_start": now - timedelta(minutes=5), "interval_end": end,
              "is_synthetic": False, "source_version": "test", "confidence": "nominal"}
    weather = SimpleNamespace(id=uuid4(), **common)
    solar = SimpleNamespace(
        predicted_power_kw=Decimal(pv), based_on_weather_forecast_id=weather.id, **common
    )
    load = SimpleNamespace(
        base_load_kw=Decimal(1),
        ev_component_kw=Decimal(0),
        flexible_component_kw=Decimal(0),
        is_cold_start=False,
        **common,
    )
    monkeypatch.setattr(
        service,
        "latest_batch",
        lambda db, model, *args: (now, [solar if model is service.PvForecast else load]),
    )
    db = Mock()
    db.scalar.return_value = None
    db.scalars.return_value = [weather]
    result = service.calculate(db, station, now=now)
    assert result.points[1].soc_percent == Decimal(expected_first_soc)
    assert result.current_rate.status == rate_status
    assert result.solar.reaches_target_at is not None and result.solar.end_soc_percent == 100
    assert result.quality == "estimated"
    weather.is_synthetic = True
    result = service.calculate(db, station, now=now)
    assert result.solar.status == "unavailable"
    assert "synthetic_forecast" in result.flags
