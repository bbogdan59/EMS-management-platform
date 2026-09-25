"""Regression tests for authorization at the command dispatch boundary."""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.security import utcnow
from app.models.device import Device
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.preference import PreferenceVersion
from app.models.station import Station, StationConfigVersion
from app.services import command_dispatch_service as dispatch
from app.services import device_service, optimization_service


def context():
    now = utcnow()
    station = Station(id=uuid.uuid4(), execution_mode="live", is_active=True)
    config = StationConfigVersion(id=uuid.uuid4())
    pref = PreferenceVersion(id=uuid.uuid4(), automation_suspended_until=None)
    device = Device(id=uuid.uuid4(), station_id=station.id, status="active")
    run = OptimizationRun(
        status="succeeded",
        is_fallback=False,
        station_config_version_id=config.id,
        preference_version_id=pref.id,
    )
    plan = Plan(
        id=uuid.uuid4(),
        station_id=station.id,
        version=1,
        status="accepted_by_device",
        execution_mode="live",
        accepted_by_device_id=device.id,
        optimization_run=run,
    )
    interval = PlanInterval(
        id=uuid.uuid4(),
        plan_id=plan.id,
        interval_start=now - timedelta(minutes=1),
        interval_end=now + timedelta(minutes=14),
        battery_soc_target_percent=60,
        battery_power_target_kw=1,
        explanation="test",
    )
    db = MagicMock()
    return now, station, config, pref, device, plan, interval, db


@pytest.mark.parametrize(
    "change",
    [
        "shadow_station",
        "shadow_plan",
        "fallback",
        "failed",
        "inactive",
        "suspended",
        "new_config",
        "new_preference",
        "superseded",
    ],
)
def test_dispatch_rejects_non_executable_plan(change):
    now, station, config, pref, device, plan, interval, db = context()
    if change == "shadow_station":
        station.execution_mode = "shadow"
    if change == "shadow_plan":
        plan.execution_mode = "shadow"
    if change == "fallback":
        plan.optimization_run.is_fallback = True
    if change == "failed":
        plan.optimization_run.status = "failed"
    if change == "inactive":
        station.is_active = False
    if change == "suspended":
        pref.automation_suspended_until = now + timedelta(hours=1)
    if change == "new_config":
        config.id = uuid.uuid4()
    if change == "new_preference":
        pref.id = uuid.uuid4()
    if change == "superseded":
        plan.status = "superseded"
    db.scalar.side_effect = [pref, config]
    assert not dispatch.plan_allows_dispatch(db, plan, station, now)


@pytest.mark.parametrize("org_status", ["suspended", "archived"])
def test_dispatch_rejects_plan_when_organization_not_active(org_status):
    """Issue #24: o organizatie suspendata/arhivata nu poate primi comenzi
    live, indiferent de starea proprie (activa) a statiei."""
    from app.models.organization import Organization

    now, station, config, pref, device, plan, interval, db = context()
    station.organization = Organization(status=org_status)
    db.scalar.side_effect = [pref, config]
    assert not dispatch.plan_allows_dispatch(db, plan, station, now)


def test_dispatch_allows_plan_when_organization_active():
    from app.models.organization import Organization

    now, station, config, pref, device, plan, interval, db = context()
    station.organization = Organization(status="active")
    db.scalar.side_effect = [pref, config]
    assert dispatch.plan_allows_dispatch(db, plan, station, now)


def test_dispatch_targets_the_device_that_accepted_the_plan(monkeypatch):
    from app.services import control_service
    monkeypatch.setattr(control_service, "execution_authorized", lambda *args: True)
    now, station, config, pref, device, plan, interval, db = context()
    db.scalars.return_value.all.return_value = [station]
    db.scalar.side_effect = [station, plan, pref, config, interval, None]
    db.get.return_value = device
    commands = dispatch.dispatch_due_commands(db)
    db.get.assert_called_once_with(Device, plan.accepted_by_device_id)
    assert len(commands) == 1
    assert commands[0].device_id == device.id
    assert commands[0].expires_at == interval.interval_end


@pytest.mark.parametrize("state", ["revoked", "wrong_station", "missing"])
def test_dispatch_does_not_fall_back_to_another_device(state):
    now, station, config, pref, device, plan, interval, db = context()
    if state == "revoked":
        device.status = "revoked"
    if state == "wrong_station":
        device.station_id = uuid.uuid4()
    db.scalars.return_value.all.return_value = [station]
    db.scalar.side_effect = [station, plan, pref, config, interval]
    db.get.return_value = None if state == "missing" else device
    assert dispatch.dispatch_due_commands(db) == []


def test_fallback_publication_is_shadow_even_for_live_station():
    now, station, config, pref, device, plan, interval, db = context()
    db.scalar.return_value = None
    published = optimization_service._publish_plan(
        db, plan.optimization_run, station, [], {}, "cost", fallback_reason="missing forecasts"
    )
    assert published.execution_mode == "shadow"
    assert station.execution_mode == "live"


def test_pending_optimizer_command_is_withdrawn_when_plan_is_superseded():
    now, station, config, pref, device, plan, interval, db = context()
    plan.status = "superseded"
    command = SimpleNamespace(
        type="hold_battery",
        id=uuid.uuid4(),
        plan_interval_id=interval.id,
        device_id=device.id,
        station_id=station.id,
        author="optimizer",
        status="created",
    )
    db.scalars.side_effect = [
        SimpleNamespace(all=lambda: []),
        SimpleNamespace(all=lambda: [command]),
    ]
    db.get.side_effect = lambda model, key: {PlanInterval: interval, Plan: plan, Station: station}[
        model
    ]
    assert device_service.list_pending_commands(db, device) == []
    assert command.status == "superseded"


def test_command_cannot_be_accepted_before_valid_from():
    now, station, config, pref, device, plan, interval, db = context()
    command = SimpleNamespace(
        device_id=device.id, status="created", valid_from=now + timedelta(hours=1)
    )
    db.scalar.side_effect = [station, command]
    with pytest.raises(device_service.DeviceServiceError, match="inca valabila"):
        device_service.acknowledge_command(db, device, uuid.uuid4(), "accepted", None)
