from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.flexible_load import (
    ComfortWindow,
    FlexibleLoadCapabilities,
    FlexibleLoadPolicy,
    ThermalModelParameters,
    ThermalSimulationRequest,
    ThermalSimulationStep,
)
from app.services.flexible_load_simulator import simulate_thermal_response


def _capabilities(**overrides):
    values = {
        "modes": {"off", "heat", "cool"},
        "rated_input_power_kw": "3",
        "minimum_setpoint_c": "16",
        "maximum_setpoint_c": "28",
        "setpoint_step_c": "0.5",
        "minimum_on_minutes": 20,
        "minimum_off_minutes": 10,
    }
    values.update(overrides)
    return FlexibleLoadCapabilities.model_validate(values)


def _model():
    return ThermalModelParameters(
        time_constant_hours=Decimal("10"),
        thermal_capacity_kwh_per_c=Decimal("5"),
        coefficient_of_performance=Decimal("3"),
    )


def _step(start, *, mode="heat", power="2", price="1"):
    return ThermalSimulationStep(
        starts_at=start,
        ends_at=start + timedelta(minutes=15),
        outdoor_temperature_c=Decimal("5"),
        requested_mode=mode,
        requested_input_power_kw=Decimal(power),
        import_price_lei_kwh=None if price is None else Decimal(price),
    )


def test_heating_response_and_cost_are_deterministic_decimal_values():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = simulate_thermal_response(
        ThermalSimulationRequest(
            initial_indoor_temperature_c=Decimal("20"),
            capabilities=_capabilities(),
            model=_model(),
            steps=[_step(start)],
        )
    )

    assert result.total_input_energy_kwh == Decimal("0.50")
    assert result.total_cost_lei == Decimal("0.50")
    # pierdere pasiva -0.375 C + castig termic 0.3 C
    assert result.steps[0].indoor_temperature_end_c == Decimal("19.925")


def test_missing_price_keeps_total_cost_unknown_not_zero():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = simulate_thermal_response(
        ThermalSimulationRequest(
            initial_indoor_temperature_c=Decimal("20"),
            capabilities=_capabilities(),
            model=_model(),
            steps=[_step(start, price=None)],
        )
    )

    assert result.total_input_energy_kwh == Decimal("0.50")
    assert result.steps[0].cost_lei is None
    assert result.total_cost_lei is None
    assert result.has_unknown_cost is True


def test_short_cycle_is_reported_without_rewriting_requested_schedule():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    steps = [
        _step(start, mode="heat", power="2"),
        _step(start + timedelta(minutes=15), mode="off", power="0"),
    ]
    result = simulate_thermal_response(
        ThermalSimulationRequest(
            initial_indoor_temperature_c=Decimal("20"),
            capabilities=_capabilities(minimum_on_minutes=20),
            model=_model(),
            steps=steps,
        )
    )

    assert result.steps[1].runtime_violation is not None
    assert "minim 20" in result.steps[1].runtime_violation
    assert result.steps[1].input_energy_kwh == 0


def test_schedule_must_be_contiguous_and_timezone_aware():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="contigui"):
        ThermalSimulationRequest(
            initial_indoor_temperature_c=Decimal("20"),
            capabilities=_capabilities(),
            model=_model(),
            steps=[_step(start), _step(start + timedelta(minutes=30))],
        )

    with pytest.raises(ValidationError, match="fusul orar"):
        _step(datetime(2026, 1, 1))


def test_unsupported_mode_and_excess_power_are_rejected_not_clamped():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="nu este raportat"):
        ThermalSimulationRequest(
            initial_indoor_temperature_c=Decimal("20"),
            capabilities=_capabilities(modes={"off", "heat"}),
            model=_model(),
            steps=[_step(start, mode="cool")],
        )

    with pytest.raises(ValidationError, match="depaseste"):
        ThermalSimulationRequest(
            initial_indoor_temperature_c=Decimal("20"),
            capabilities=_capabilities(),
            model=_model(),
            steps=[_step(start, power="4")],
        )


def test_auto_with_power_is_unknown_direction_and_refused():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    request = ThermalSimulationRequest(
        initial_indoor_temperature_c=Decimal("20"),
        capabilities=_capabilities(modes={"off", "auto"}),
        model=_model(),
        steps=[_step(start, mode="auto", power="2")],
    )
    with pytest.raises(ValueError, match="directie/feedback"):
        simulate_thermal_response(request)


def test_overlapping_comfort_windows_and_naive_override_are_rejected():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = ComfortWindow(
        starts_at=start,
        ends_at=start + timedelta(hours=2),
        minimum_temperature_c=Decimal("20"),
        maximum_temperature_c=Decimal("22"),
    )
    second = ComfortWindow(
        starts_at=start + timedelta(hours=1),
        ends_at=start + timedelta(hours=3),
        minimum_temperature_c=Decimal("18"),
        maximum_temperature_c=Decimal("22"),
    )
    with pytest.raises(ValidationError, match="suprapune"):
        FlexibleLoadPolicy(enabled=True, comfort_windows=[first, second])

    with pytest.raises(ValidationError, match="fusul orar"):
        FlexibleLoadPolicy(enabled=True, override_until=datetime(2026, 1, 2))


def test_default_policy_never_authorizes_live_control():
    policy = FlexibleLoadPolicy()
    assert policy.enabled is False
    assert policy.maximum_automation_stage.value == "recommendation"
