import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import deye_cloud_service
from app.services import deye_solar_service as service

FIXTURES = json.loads(
    (Path(__file__).parents[2] / "contracts/solar/deye-capabilities.json").read_text()
)


@pytest.mark.parametrize("profile", FIXTURES["profiles"], ids=lambda profile: profile["name"])
def test_reported_capability_fixtures(profile):
    inputs = service.parse_inputs(profile)
    assert [row["index"] for row in inputs] == profile["available_inputs"]
    if len(inputs) == 1:
        assert inputs[0]["power_w"] == "610.00" and inputs[0]["kind"] == "pv_input"
    elif len(inputs) == 2:
        assert inputs[0]["power_w"] == "0" and inputs[1]["power_w"] is None
        assert "current_a" not in inputs[0]["supported_metrics"]


def test_parser_preserves_eight_inputs_invalid_values_and_unknown_units():
    data = {
        "dataList": [
            {"key": f"PV{index} Power", "unit": "W", "value": "0"} for index in range(1, 9)
        ]
    }
    assert len(service.parse_inputs(data)) == 8
    data["dataList"] = [
        {"key": "PV1 Power", "unit": "W", "value": "-1"},
        {"key": "PV2 Power", "unit": "W", "value": "Infinity"},
        {"key": "PV3 Power", "unit": "mW", "value": "1"},
    ]
    inputs = service.parse_inputs(data)
    assert len(inputs) == 2 and all(i["power_w"] is None for i in inputs)
    data["dataList"] += [
        {"key": "MPPT1 Voltage", "unit": "V", "value": "300"},
        {"key": "PV2 Power", "unit": "W", "value": "0"},
    ]
    assert service.parse_inputs(data) == []
    assert service.parse_inputs({"dataList": None}) == []


def test_read_only_poll_is_serial_scoped_and_ignores_old_or_unstamped_rows(monkeypatch):
    now = datetime(2026, 9, 29, 6, tzinfo=UTC)
    link = SimpleNamespace(
        remote_device_sn="fixture-serial", remote_device_type="INVERTER", raw_snapshot={}
    )
    connection = SimpleNamespace(device_id="fixture-device", device_links=[link])
    data = {**FIXTURES["profiles"][0], "deviceSn": link.remote_device_sn, "deviceType": "INVERTER"}
    post = Mock(
        return_value={
            "deviceDataList": [
                data,
                {**data, "deviceSn": "another-tenant", "collectionTime": now.timestamp()},
                {**data, "collectionTime": (now - timedelta(hours=1)).timestamp()},
                {**data, "collectionTime": now.timestamp()},
            ]
        }
    )
    ingest = Mock()
    monkeypatch.setattr(deye_cloud_service, "_post", post)
    monkeypatch.setattr(service, "ingest", ingest)
    assert service.poll(Mock(), connection, "fake-token", now) == 1
    post.assert_called_once_with(
        "/v1.0/device/latest",
        body={"deviceList": [link.remote_device_sn]},
        access_token="fake-token",
    )
    assert ingest.call_count == 1 and ingest.call_args.args[4] == now
    post.side_effect = deye_cloud_service.DeyeCloudError("unavailable")
    assert service.poll(Mock(), connection, "fake-token", now) == 0
