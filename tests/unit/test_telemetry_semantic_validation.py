from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.security import utcnow
from app.schemas.device_api import TelemetryItem
from app.services.device_service import telemetry_semantic_rejection


def _item(**overrides) -> TelemetryItem:
    payload = {"boot_id": "boot-semantic", "sequence": 1, "measured_at": utcnow()}
    payload.update(overrides)
    return TelemetryItem(**payload)


def test_telemetry_semantic_rejection_allows_zero_soc_and_signed_power_flows():
    item = _item(
        pv_power_w=0,
        load_power_w=0,
        battery_power_w=-500,
        grid_power_w=-250,
        battery_soc_percent=0,
        ev_power_w=0,
    )

    assert telemetry_semantic_rejection(item) is None


def test_telemetry_semantic_rejection_flags_permanent_metric_errors():
    assert telemetry_semantic_rejection(_item(pv_power_w=-1)) == "pv_power_negative"
    assert telemetry_semantic_rejection(_item(load_power_w=-1)) == "load_power_negative"
    assert telemetry_semantic_rejection(_item(ev_power_w=-1)) == "ev_power_negative"
    assert telemetry_semantic_rejection(_item(battery_soc_percent=Decimal("100.1"))) == "battery_soc_out_of_range"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pv_power_w", Decimal("NaN")),
        ("load_power_w", Decimal("Infinity")),
        ("battery_power_w", Decimal("-Infinity")),
        ("grid_power_w", Decimal("NaN")),
        ("battery_soc_percent", Decimal("NaN")),
        ("ev_power_w", Decimal("Infinity")),
    ],
)
def test_telemetry_item_rejects_nonfinite_numeric_values(field, value):
    with pytest.raises(ValidationError, match="finite number"):
        _item(**{field: value})
