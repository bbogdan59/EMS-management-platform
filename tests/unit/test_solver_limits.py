"""Exercise real HiGHS solves: an explicit zero must never mean unlimited."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import optimization_service as service


def solve(**overrides):
    horizon = [
        datetime(2026, 9, 10, 12, tzinfo=UTC) + timedelta(minutes=15 * i) for i in range(4)
    ]
    config = SimpleNamespace(
        battery_reference_capacity_kwh=10,
        battery_available_capacity_kwh=10,
        battery_max_charge_power_kw=3,
        battery_max_discharge_power_kw=3,
        battery_charge_efficiency=0.95,
        battery_discharge_efficiency=0.95,
        grid_import_limit_kw=10,
        grid_export_limit_kw=10,
        inverter_power_kw=10,  # suficient de mare cat sa nu limiteze niciunul dintre scenariile de mai jos
        ev_enabled=False,
    )
    pref = SimpleNamespace(
        min_reserve_soc_percent=10,
        max_normal_soc_percent=90,
        priority="cost",
        allow_grid_charge=True,
        allow_battery_export=True,
        soc_targets=[],
        max_efc_per_day=None,
        max_efc_per_month=None,
    )
    for key, value in overrides.items():
        setattr(config if hasattr(config, key) else pref, key, value)
    return service._solve(
        station=SimpleNamespace(timezone="Europe/Bucharest"),
        config=config,
        preference=pref,
        horizon=horizon,
        interval_minutes=15,
        pv_series=dict.fromkeys(horizon, 0),
        load_series=dict.fromkeys(horizon, 0.1),
        price_buy=dict(zip(horizon, [0.01, 0.01, 3, 3], strict=True)),
        price_sell=dict(zip(horizon, [0, 0, 2, 2], strict=True)),
        current_soc_kwh=5,
        db=MagicMock(),
    )


@pytest.mark.parametrize(
    "field",
    [
        "grid_export_limit_kw",
        "grid_import_limit_kw",
        "battery_max_charge_power_kw",
        "battery_max_discharge_power_kw",
        "max_efc_per_day",
        "max_efc_per_month",
    ],
)
def test_explicit_zero_limits_are_enforced(field, monkeypatch):
    monkeypatch.setattr(service, "get_efc_used", lambda *args: 0.0)
    weights = {**service.PRIORITY_WEIGHTS["cost"], "terminal_value": 0.0}
    monkeypatch.setitem(service.PRIORITY_WEIGHTS, "cost", weights)
    result = solve(**{field: 0})
    assert result["termination"] == "optimal"
    for interval in result["intervals"]:
        battery = interval["battery_power_target_kw"]
        grid = interval["grid_power_target_kw"]
        if field == "grid_export_limit_kw":
            assert grid >= -1e-6
        if field == "grid_import_limit_kw":
            assert grid <= 1e-6
        if field == "battery_max_charge_power_kw":
            assert battery <= 1e-6
        if field in ("battery_max_discharge_power_kw", "max_efc_per_day", "max_efc_per_month"):
            assert battery >= -1e-6
