from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import deye_cloud_service, deye_solar_service
from app.services.grid_voltage_service import parse_deye


def test_explicit_phase_and_units_only_preserve_unknown_zero_and_duplicates():
    values = [
        ("Grid Voltage L1", "V", "0"),
        ("Grid L2 Voltage", "V", "231.2"),
        ("GridVoltageL3", "V", None),
        ("Load Voltage L1", "V", "999"),
        ("MI Voltage L1", "V", "999"),
        ("Voltage L1", "V", "999"),
        ("Grid Voltage", "V", "999"),
        ("Battery Voltage", "V", "52"),
    ]
    points = [{"key": key, "unit": unit, "value": value} for key, unit, value in values]
    assert parse_deye({"dataList": points}) == {
        "L1": Decimal(0),
        "L2": Decimal("231.2"),
        "L3": None,
    }
    points.append({"key": "Grid L2 Voltage", "unit": "V", "value": "232"})
    assert parse_deye({"dataList": points})["L2"] is None
    for value in ("NaN", "Infinity", "-1", "1000000000", "bad", None):
        assert parse_deye(
            {"dataList": [{"key": "Grid L1 Voltage", "unit": "V", "value": value}]}
        ) == {"L1": None}
    assert (
        parse_deye({"dataList": [{"key": "Grid L1 Voltage", "unit": "kV", "value": "230"}]}) == {}
    )
    assert parse_deye({"dataList": None}) == {}


def test_poll_uses_own_timestamp_and_known_inverter_scope_without_extra_calls(monkeypatch):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    link = SimpleNamespace(
        id="source", remote_device_sn="fixture", remote_device_type="INVERTER", raw_snapshot={}
    )
    connection = SimpleNamespace(station_id="station", device_id="device", device_links=[link])
    at = now - timedelta(minutes=2)
    item = {
        "deviceSn": "fixture",
        "deviceType": "INVERTER",
        "collectionTime": at.timestamp(),
        "dataList": [{"key": "Grid L1 Voltage", "unit": "V", "value": "230"}],
    }
    post = Mock(
        return_value={
            "deviceDataList": [
                item,
                {**item, "deviceSn": "other"},
                {**item, "collectionTime": None},
                {**item, "collectionTime": (now - timedelta(hours=1)).timestamp()},
            ]
        }
    )
    ingest = Mock()
    monkeypatch.setattr(deye_cloud_service, "_post", post)
    monkeypatch.setattr(deye_solar_service, "ingest_deye", ingest)
    assert deye_solar_service.poll(Mock(), connection, "fixture-token", now) == 0
    post.assert_called_once_with(
        "/v1.0/device/latest", body={"deviceList": ["fixture"]}, access_token="fixture-token"
    )
    ingest.assert_called_once()
    assert ingest.call_args.args[1:] == (
        "station",
        "source",
        "Invertor Deye 1",
        at,
        now,
        {"L1": Decimal("230")},
    )
