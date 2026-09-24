from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.api.v1.telemetry import TELEMETRY_CONTRACT_V1
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


def test_telemetry_contract_declares_v1_metrics_and_ack_semantics():
    contract = TELEMETRY_CONTRACT_V1
    metrics = {metric["name"]: metric for metric in contract["metrics"]}

    assert contract["schema_version"] == 1
    assert contract["deduplication_key"] == ["device_id", "boot_id", "sequence"]
    assert metrics["battery_soc_percent"]["min_value"] == 0
    assert metrics["battery_soc_percent"]["max_value"] == 100
    assert metrics["battery_soc_percent"]["nullable"] is True
    assert metrics["grid_power_w"]["sign"] == "positive_import_negative_export"
    assert metrics["battery_power_w"]["sign"] == "positive_charge_negative_discharge"
    assert contract["provenance"]["numeric_values_default"] == "measured"
    assert contract["provenance"]["simulation_flag"] == "raw_payload.simulated == true OR quality_flags.simulated == true"
    assert "derived" in contract["provenance"]["categories"]
    assert "unsupported raw_payload fields" in contract["provenance"]["rule"]
    assert contract["raw_payload"]["purpose"].startswith("diagnostic/source payload only")
    assert contract["ack"]["ordered"] is True
    assert "future_timestamp" in contract["ack"]["retryable_reason_codes"]
    assert "pv_power_negative" in contract["ack"]["permanent_reason_codes"]
