from __future__ import annotations

from pydantic import ValidationError

from app.schemas.device_api import TelemetryItem


def test_telemetry_item_accepts_typed_extended_metrics():
    item = TelemetryItem.model_validate(
        {
            "boot_id": "boot-1",
            "sequence": 1,
            "measured_at": "2026-09-18T10:00:00Z",
            "mppt": [{"index": 1, "voltage_v": "390.2", "current_a": "4.8", "power_w": "1873.0"}],
            "phases": [{"phase": "L1", "voltage_v": "230.1", "current_a": "3.4", "active_power_w": "782"}],
            "battery": {"voltage_v": "51.8", "current_a": "-5.8", "temperature_c": "28.4", "state": "discharging"},
            "status": {
                "inverter_state": "running",
                "battery_state": "discharging",
                "faults": [{"code": "grid_warn", "severity": "warning", "message": "Voltage high"}],
            },
            "counters": [{"name": "pv_energy_total", "value": "12345.678", "unit": "kWh", "reset_id": "meter-1"}],
        }
    )

    assert item.mppt[0].quality == "measured"
    assert item.status is not None
    assert item.status.quality == "reported"
    assert item.counters[0].reset_id == "meter-1"


def test_telemetry_item_rejects_unknown_extended_fields_and_non_finite_values():
    try:
        TelemetryItem.model_validate(
            {
                "boot_id": "boot-1",
                "sequence": 1,
                "measured_at": "2026-09-18T10:00:00Z",
                "mppt": [{"index": 1, "power_w": "NaN", "mystery": 1}],
            }
        )
    except ValidationError as exc:
        errors = exc.errors()
    else:
        raise AssertionError("extended telemetry with NaN and unknown fields must be rejected")

    assert any(error["type"] == "extra_forbidden" for error in errors)
    assert any(error["type"] == "finite_number" for error in errors)
