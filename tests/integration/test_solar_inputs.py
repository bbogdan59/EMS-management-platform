from datetime import timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.core.security import utcnow
from app.models.audit import AuditLog
from app.models.solar import SolarInputAggregate, SolarInputSample, SolarTracker
from app.models.telemetry import TelemetryRaw
from app.schemas.device_api import MpptTelemetry, TelemetryItem
from app.services import device_service
from app.services import solar_service as service
from scripts.backfill_solar_inputs import replay_day
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.web_helpers import login


@pytest.fixture()
def context(db):
    user = make_user(db)
    org = make_org(db)
    member = make_membership(db, user, org, "organization_admin")
    station = make_station(db, org, user)
    device = make_device(db, station)
    at = utcnow().replace(second=0, microsecond=0)
    return user, org, member, station, device, at


def ingest(db, device, at, inputs, **kwargs):
    return service.ingest(db, device, "device_rs485", "primary", at, at, inputs, **kwargs)


def test_replay_retained_raw_is_idempotent_and_keeps_historical_station(db, context):
    user, org, member, station, device, at = context
    db.add(
        TelemetryRaw(
            station_id=station.id,
            device_id=device.id,
            boot_id="before-upgrade",
            sequence=1,
            measured_at=at,
            received_at=at,
            diagnostics={
                "mppt": [{"index": 1, "power_w": "0", "voltage_v": None}],
                "inverter": {"ac_output_power_w": "-10", "quality": "simulated"},
            },
        )
    )
    moved = make_station(db, org, user, name="Device moved here")
    device.station_id = moved.id
    db.flush()
    for _ in range(2):
        assert replay_day(db, station, at - timedelta(minutes=1), at + timedelta(minutes=1)) == 1
    assert db.scalar(select(func.count(SolarInputSample.id))) == 1
    assert service.snapshot(db, moved, at)["inverters"] == []
    result = service.snapshot(db, station, at)["inverters"][0]
    assert result["inputs"][0]["metrics"]["voltage_v"]["value"] is None
    assert Decimal(result["dc_total_w"]) == 0
    assert result["ac_output"]["quality"] == "simulated"
    assert Decimal(result["ac_output"]["value"]) == -10


@pytest.mark.parametrize("count", [1, 2, 8])
def test_device_ingestion_is_variable_count_and_retry_idempotent(db, context, count):
    user, org, member, station, device, at = context
    item = TelemetryItem(
        schema_version=1,
        boot_id="solar",
        sequence=1,
        measured_at=at,
        mppt=[
            {
                "index": i,
                "voltage_v": "300",
                "power_w": "0" if i == 1 else None,
                "supported_metrics": ["voltage_v", "power_w"],
            }
            for i in range(1, count + 1)
        ],
    )
    assert device_service.ingest_telemetry_batch(db, device, [item])[0] == 1
    assert device_service.ingest_telemetry_batch(db, device, [item])[1] == 1
    assert db.scalar(select(func.count(SolarInputSample.id))) == count
    view = service.snapshot(db, station, at)["inverters"][0]
    assert len(view["inputs"]) == count
    first = view["inputs"][0]
    assert Decimal(first["metrics"]["power_w"]["value"]) == 0
    assert first["metrics"]["current_a"] == {
        "value": None,
        "unit": "A",
        "supported": False,
        "quality": "missing",
    }
    assert (view["dc_total_w"] is None) == (count > 1)
    assert view["ac_output"]["value"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"power_w": "-1"},
        {"voltage_v": "NaN"},
        {"current_a": "Infinity"},
        {"index": 2147483648},
        {"power_w": "1", "supported_metrics": []},
    ],
)
def test_invalid_units_sign_or_capability_rejected(change):
    with pytest.raises(ValidationError):
        MpptTelemetry.model_validate({"index": 1, **change})


def test_delayed_samples_do_not_replace_latest_capabilities_and_missing_input_is_stale(db, context):
    user, org, member, station, device, at = context
    ingest(
        db,
        device,
        at,
        [{"index": 1, "power_w": "100"}, {"index": 2, "power_w": "200"}],
        ac_power="275",
    )
    ingest(
        db,
        device,
        at + timedelta(minutes=1),
        [{"index": 1, "power_w": "0", "supported_metrics": ["power_w"]}],
        ac_power="0",
    )
    ingest(
        db,
        device,
        at - timedelta(minutes=1),
        [{"index": 1, "voltage_v": "300"}],
        flags={"simulated", "stale"},
    )
    view = service.snapshot(db, station, at + timedelta(minutes=1))["inverters"][0]
    assert view["inputs"][0]["metrics"]["voltage_v"]["supported"] is False
    assert view["inputs"][1]["freshness"] == "stale" and view["dc_total_w"] is None
    old = service.snapshot(db, station, at + timedelta(hours=1))["inverters"][0]
    assert old["ac_output"]["quality"] == "stale"
    assert Decimal(old["ac_output"]["value"]) == 0


def test_carry_in_flags_null_breaks_and_time_weighted_aggregation(db, context):
    user, org, member, station, device, at = context
    start = at.replace(minute=0)
    ingest(
        db,
        device,
        start - timedelta(minutes=1),
        [{"index": 1, "power_w": "600", "voltage_v": "300"}],
        flags={"simulated", "stale"},
    )
    ingest(
        db,
        device,
        start + timedelta(minutes=1),
        [{"index": 1, "power_w": None, "voltage_v": "310"}],
    )
    ingest(
        db, device, start + timedelta(minutes=2), [{"index": 1, "power_w": "0", "voltage_v": "320"}]
    )
    service.reaggregate(db, station, start, start + timedelta(minutes=15))
    row = db.scalar(
        select(SolarInputAggregate).where(
            SolarInputAggregate.period_type == "15m", SolarInputAggregate.period_start == start
        )
    )
    assert row.energy_kwh == Decimal("0.010000")
    assert set(row.flags) == {"simulated", "stale"} and row.quality == "simulated"
    assert Decimal(row.coverage["power_w"]) < Decimal(row.coverage["voltage_v"])
    assert row.current_a is None and row.coverage["current_a"] == "0"
    original = (row.energy_kwh, row.power_w, row.flags)
    service.reaggregate(db, station, start, start + timedelta(minutes=15))
    db.refresh(row)
    assert (row.energy_kwh, row.power_w, row.flags) == original
    history = service.history(db, station, row.tracker_id, now=start + timedelta(hours=2))
    assert len(history["points"]) == 96
    assert any(p["power_w"] is None and p["quality"] == "missing" for p in history["points"])
    assert any(p["power_w"] is not None and "simulated" in p["flags"] for p in history["points"])


def test_comparison_requires_configured_orientation_capacity_and_measured_data(db, context):
    user, org, member, station, device, at = context
    ingest(db, device, at, [{"index": 1, "power_w": "1000"}, {"index": 2, "power_w": "100"}])
    assert service.snapshot(db, station, at)["inverters"][0]["warnings"] == []
    trackers = db.scalars(select(SolarTracker).order_by(SolarTracker.input_index)).all()
    config = {
        "comparison_group": "south",
        "azimuth_deg": "180",
        "tilt_deg": "30",
        "installed_kw": "2",
        "warning_threshold_percent": "30",
    }
    for tracker in trackers:
        tracker.configuration = dict(config)
    db.flush()
    assert (
        service.snapshot(db, station, at)["inverters"][0]["warnings"][0]["difference_percent"]
        == "90.0"
    )
    trackers[1].configuration = {**config, "installed_kw": "0.2"}
    assert service.snapshot(db, station, at)["inverters"][0]["warnings"] == []
    trackers[1].configuration = {**config, "azimuth_deg": "90"}
    assert service.snapshot(db, station, at)["inverters"][0]["warnings"] == []
    trackers[1].configuration = dict(config)
    ingest(
        db,
        device,
        at + timedelta(minutes=1),
        [{"index": 1, "power_w": "1000"}, {"index": 2, "power_w": "100", "quality": "simulated"}],
    )
    assert (
        service.snapshot(db, station, at + timedelta(minutes=1))["inverters"][0]["warnings"] == []
    )


def test_snapshot_history_and_configuration_scope_csrf_revision_audit(db, client, context):
    user, org, member, station, device, at = context
    ingest(db, device, at, [{"index": 1, "power_w": "0"}])
    second = make_station(db, org, user, name="Other roof")
    other_device = make_device(db, second)
    ingest(db, other_device, at, [{"index": 1, "power_w": "10"}])
    foreign = service.snapshot(db, second, at)["inverters"][0]["inputs"][0]["id"]
    db.commit()
    login(client, user.email, "TestPass1234")
    base = f"/api/v1/stations/{station.id}/solar"
    response = client.get(base)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    entry = response.json()["inverters"][0]["inputs"][0]
    target = base + f"/trackers/{entry['id']}"
    assert client.get(base + f"/trackers/{foreign}/history").status_code == 404
    assert client.get(target + "/history?range=24h").status_code == 200
    data = {
        "revision": 0,
        "label": "Acoperis sud",
        "strings": [{"identifier": "south-1", "label": "Sir sud"}],
    }
    assert client.put(target, json=data).status_code == 403
    headers = {"X-CSRF-Token": client.cookies.get("ems_csrf")}
    assert client.put(target, json=data, headers=headers).json() == {
        "revision": 1,
        "physical_write": False,
    }
    assert client.put(target, json=data, headers=headers).status_code == 409
    assert client.put(base + f"/trackers/{foreign}", json=data, headers=headers).status_code == 404
    assert db.scalar(select(AuditLog).where(AuditLog.action == "solar.tracker_configured"))
    member.role = "viewer"
    db.commit()
    assert client.put(target, json={**data, "revision": 1}, headers=headers).status_code == 403
    member.is_active = False
    db.commit()
    assert client.get(base).status_code == 403


def test_migration_roundtrip_retains_mixed_null_zero_and_nonzero_values(db, context, monkeypatch):
    user, org, member, station, device, at = context
    ingest(
        db,
        device,
        at,
        [{"index": 1, "voltage_v": "300", "power_w": "0"}, {"index": 2, "current_a": "2.5"}],
    )
    service.reaggregate(db, station, at - timedelta(hours=1), at)
    rows = db.scalars(select(SolarInputSample).order_by(SolarInputSample.id)).all()
    saved = [(r.id, r.voltage_v, r.current_a, r.power_w) for r in rows]
    energy = db.execute(
        text("SELECT id, energy_kwh, coverage FROM solar_input_aggregates ORDER BY id")
    ).all()
    path = (
        Path(__file__).parents[2] / "alembic/versions/b320c221d901_solar_inputs_morning_briefing.py"
    )
    spec = spec_from_file_location("solar_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db.connection())))
    migration.downgrade()
    assert (
        db.execute(
            text(
                "SELECT id, voltage_v, current_a, power_w FROM legacy_solar_input_samples ORDER BY id"
            )
        ).all()
        == saved
    )
    assert (
        db.execute(
            text("SELECT id, energy_kwh, coverage FROM legacy_solar_input_aggregates ORDER BY id")
        ).all()
        == energy
    )
    migration.upgrade()
    assert (
        db.execute(
            text("SELECT id, voltage_v, current_a, power_w FROM solar_input_samples ORDER BY id")
        ).all()
        == saved
    )
    assert (
        db.execute(
            text("SELECT id, energy_kwh, coverage FROM solar_input_aggregates ORDER BY id")
        ).all()
        == energy
    )
