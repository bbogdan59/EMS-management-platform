from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import get_settings
from app.core.security import hash_password
from app.models.device import DeviceCredential
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.telemetry import TelemetryRaw
from app.schemas.energy_operations import (
    ControlPolicyIn,
)
from app.services import command_dispatch_service as dispatch
from app.services import control_service as control
from app.services import device_service
from app.services import ev_service as ev
from app.services import recommendation_service as recommendations
from tests.factories import make_device, make_membership, make_org, make_station, make_user


def make_control_context(db, monkeypatch):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    for module in (control, ev, recommendations, device_service, dispatch):
        monkeypatch.setattr(module, "utcnow", lambda: now)
    user = make_user(db, email=f"operations-{uuid4().hex}@test.local")
    org = make_org(db, f"Operations {uuid4().hex}")
    make_membership(db, user, org, "organization_admin")
    station = make_station(db, org, user, timezone="UTC")
    device = make_device(db, station)
    device.last_heartbeat_at = now
    device.firmware_version = "test-agent"
    device.capabilities = {"inverter_write": True, "closed_loop": {
        "schema_version": 1, "readback": True, "commands": ["set_battery_target_soc"],
        "profile_id": "test-only", "model": "test-model", "inverter_firmware": "test-firmware",
    }}
    settings = get_settings()
    monkeypatch.setattr(settings, "closed_loop_execution_enabled", True)
    monkeypatch.setattr(settings, "closed_loop_verified_profiles", {"test-only": {
        "hardware_verified": True, "model": "test-model", "agent_versions": ["test-agent"],
        "inverter_firmware_versions": ["test-firmware"],
    }})
    db.add(DeviceCredential(device_id=device.id, secret_hash=hash_password("ops-secret")))
    db.add(TelemetryRaw(station_id=station.id, device_id=device.id, boot_id="ops", sequence=1,
                        measured_at=now, received_at=now, battery_soc_percent=50, battery_power_w=0,
                        grid_power_w=0, load_power_w=1000, pv_power_w=1000))
    db.flush()
    config, preference = control.current_versions(db, station)
    run = OptimizationRun(station_id=station.id, status="succeeded", is_fallback=False,
                          horizon_start=now + timedelta(minutes=1), horizon_end=now + timedelta(minutes=16),
                          station_config_version_id=config.id, preference_version_id=preference.id,
                          objective_value_lei=Decimal(".05"),
                          input_snapshot={"soc": {"quality": "measured", "kwh": 5},
                                          "pv_forecast_quality": {"1": "measured"},
                                          "load_forecast_quality": {"1": "measured"},
                                          "price_buy_quality": {"1": "measured"}})
    db.add(run)
    db.flush()
    plan = Plan(station_id=station.id, optimization_run=run, version=1, status="published", execution_mode="shadow")
    db.add(plan)
    db.flush()
    interval = PlanInterval(plan_id=plan.id, interval_start=run.horizon_start, interval_end=run.horizon_end,
                            battery_power_target_kw=Decimal(".5"), battery_soc_target_percent=Decimal("51"),
                            grid_power_target_kw=Decimal(".5"), pv_forecast_kw=1, load_forecast_kw=1,
                            ev_charge_power_kw=0, price_import_lei_kwh=1, price_export_lei_kwh=Decimal(".5"))
    db.add(interval)
    db.flush()
    policy = control.save_policy(db, station, user, ControlPolicyIn(
        device_id=device.id, min_soc_percent=15, max_soc_percent=95, max_managed_energy_kwh=2,
        max_charge_kw=2, max_discharge_kw=2, max_ramp_kw_per_minute=1, max_efc_day=2, max_efc_month=30,
        windows=[{"days": list(range(7)), "start_minute": 0, "end_minute": 1440}], expires_at=now + timedelta(days=1),
    ))
    state = control.control_state(db, station)
    control.change_mode(db, station, user, "assisted", "Test fixture only", state.revision, True)
    db.flush()
    return user, station, device, plan, interval, policy, now


def approve(db, ops):
    user, station, device, plan, interval, policy, now = ops
    preview = control.preview(db, station, plan, now)
    assert preview["reasons"] == []
    return control.approve_plan(db, station, plan, user, preview["preview_hash"], now)


def command_for(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    approve(db, ops)
    device_service.accept_plan(db, device, plan.version)
    monkeypatch.setattr(dispatch, "utcnow", lambda: now + timedelta(minutes=1))
    return dispatch.dispatch_due_commands(db)[0]



@pytest.fixture(name="ops")
def ops_fixture(db, monkeypatch):
    return make_control_context(db, monkeypatch)
