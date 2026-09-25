from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.core.rate_limit import reset_key
from app.models.command import Command
from app.models.control import CommandVerification, PlanApproval
from app.models.ev import EVSE, ChargingSession, EVObservation
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryRaw
from app.schemas.energy_operations import (
    EVObservationIn,
    EVRequirementIn,
    EVSEIn,
    ReadbackIn,
    VehicleIn,
)
from app.services import command_dispatch_service as dispatch
from app.services import control_service as control
from app.services import device_service
from app.services import ev_analytics_service as analytics
from app.services import ev_service as ev
from app.services import recommendation_service as recommendations
from tests.energy_helpers import approve, command_for, ops_fixture  # noqa: F401
from tests.factories import make_org, make_station, make_user
from tests.web_helpers import login


def test_approval_is_immutable_and_ack_is_not_readback(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    approval = approve(db, ops)
    assert approve(db, ops).id == approval.id
    assert db.scalar(select(func.count(PlanApproval.id))) == 1
    command = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    device_service.list_pending_commands(db, device)
    device_service.acknowledge_command(db, device, command.id, "accepted", None)
    assert db.scalar(select(CommandVerification).where(CommandVerification.command_id == command.id)) is None
    device_service.report_command_result(db, device, command.id, "executed", {"applied": True}, None)
    verification = db.scalar(select(CommandVerification).where(CommandVerification.command_id == command.id))
    assert verification.status == "applied" and verification.read_back_at is None
    data = ReadbackIn(observed_at=now + timedelta(minutes=1), command_version=1,
                      idempotency_key=command.idempotency_key, target_soc_percent=51, battery_power_kw=".5")
    control.verify_readback(db, device, command.id, data, now + timedelta(minutes=1))
    assert verification.status == "read_back_verified"
    assert control.verify_readback(db, device, command.id, data, now + timedelta(minutes=1)).id == verification.id
    with pytest.raises(ValueError, match="contradictoriu"):
        control.verify_readback(db, device, command.id, data.model_copy(update={"battery_power_kw": Decimal(1)}), now + timedelta(minutes=1))


@pytest.mark.parametrize("change,reason", [("zero_limit", "battery_limit"), ("stale", "fresh_soc_power_required"),
                                          ("offline", "device_offline"), ("unknown_efc", "efc_history_unknown"),
                                          ("read_only", "read_only_device"), ("simulated", "fresh_soc_power_required")])
def test_policy_fails_closed_for_missing_inputs_and_explicit_limits(db, ops, change, reason):
    user, station, device, plan, interval, policy, now = ops
    if change == "zero_limit":
        policy.limits = {**policy.limits, "max_charge_kw": "0"}
    elif change == "offline":
        device.last_heartbeat_at = now - timedelta(hours=1)
    elif change == "read_only":
        device.capabilities = {"inverter_write": False}
    elif change == "unknown_efc":
        station.timezone = "Europe/Bucharest"
    else:
        raw = db.scalar(select(TelemetryRaw).where(TelemetryRaw.station_id == station.id))
        raw.quality_flags = {change: True}
    db.flush()
    assert reason in control.policy_check(db, station, plan, policy, now)
    with pytest.raises(ValueError):
        control.approve_plan(db, station, plan, user, control.preview(db, station, plan)["preview_hash"])


def test_changed_plan_or_mode_revokes_dispatch_authorization(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    command = command_for(db, ops, monkeypatch)
    interval.battery_power_target_kw = Decimal(".75")
    db.flush()
    assert not dispatch.command_allows_delivery(db, command, device, now + timedelta(minutes=1))
    control.suspend(db, station, "emergency", user)
    assert command.status == "superseded" and station.execution_mode == "shadow"
    assert not dispatch.dispatch_due_commands(db)


def test_readback_mismatch_and_timeout_suspend_automation(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    command = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    device_service.acknowledge_command(db, device, command.id, "accepted", None)
    device_service.report_command_result(db, device, command.id, "executed", {}, None)
    verification = control.verify_readback(db, device, command.id, ReadbackIn(
        observed_at=now + timedelta(minutes=1), command_version=1, idempotency_key=command.idempotency_key,
        target_soc_percent=80, battery_power_kw=1,
    ), now + timedelta(minutes=1))
    assert verification.status == "read_back_mismatch"
    assert control.control_state(db, station).mode == "suspended"


def test_expired_result_and_reconnect_never_execute_old_command(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    command = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    device_service.acknowledge_command(db, device, command.id, "accepted", None)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(hours=1))
    with pytest.raises(device_service.DeviceServiceError):
        device_service.report_command_result(db, device, command.id, "executed", {}, None)
    assert device_service.list_pending_commands(db, device) == []
    assert command.status == "expired"
    control.reconcile_station(db, station, now + timedelta(hours=1))
    assert control.control_state(db, station).mode == "suspended"


def test_ev_meter_reset_duplicate_out_of_order_and_missing_soc(db, ops):
    user, station, device, plan, interval, policy, now = ops
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="EV", device_id=device.id, max_power_kw=7, capabilities={"meter": True}))
    for index, (state, meter, epoch) in enumerate((("connected", 100, "a"), ("charging", 101, "a"), ("paused", 1, "b"), ("disconnected", 2, "b"))):
        data = EVObservationIn(event_id=str(index), observed_at=now + timedelta(minutes=index * 5), state=state,
                               meter_kwh=meter, meter_epoch=epoch, power_kw=7)
        observation, status = ev.ingest_observation(db, device, connector.id, data, data.observed_at)
        assert status == "accepted"
        assert ev.ingest_observation(db, device, connector.id, data, data.observed_at)[1] == "duplicate"
    session = db.scalar(select(ChargingSession).where(ChargingSession.connector_id == connector.id))
    assert session.energy_kwh is None and "meter_reset" in session.reason_codes
    assert session.vehicle_id is None and session.ended_at is not None
    assert analytics.session_summary(db, session)["known_energy_kwh"] == 2
    late = EVObservationIn(event_id="late", observed_at=now + timedelta(minutes=2), state="charging", meter_kwh=Decimal("100.5"))
    assert ev.ingest_observation(db, device, connector.id, late, now + timedelta(minutes=20))[1] == "retained_out_of_order"
    assert connector.state == "disconnected" and db.scalar(select(func.count(ChargingSession.id))) == 1


def test_ev_historical_cost_and_zero_energy_are_decimal(db, ops):
    user, station, device, plan, interval, policy, now = ops
    tariff = Tariff(station_id=station.id, direction="import", kind="fixed", name="Test")
    db.add(tariff)
    db.flush()
    db.add(TariffVersion(tariff_id=tariff.id, valid_from=now - timedelta(days=1), fixed_price_lei_per_kwh=Decimal("1.25")))
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="EV", device_id=device.id, max_power_kw=4, capabilities={"meter": True}))
    for index, (state, meter) in enumerate((("connected", 0), ("charging", 0), ("completed", 2))):
        timestamp = now + timedelta(minutes=index * 15)
        ev.ingest_observation(db, device, connector.id, EVObservationIn(event_id=str(index), observed_at=timestamp, state=state, meter_kwh=meter), timestamp)
    session = db.scalar(select(ChargingSession).where(ChargingSession.connector_id == connector.id))
    result = analytics.session_summary(db, session)
    assert result["total_energy_kwh"] == Decimal(2)
    assert result["estimated_cost_lei"] == Decimal("2.50") and result["average_cost_lei_kwh"] == Decimal("1.25")
    assert result["energy_coverage"] == 1 and result["cost_100km_lei"] is None
    assert result["optimization_savings_lei"] is None


def test_ev_schedule_dst_leap_and_one_off_exception(db, ops):
    user, station, device, plan, interval, policy, now = ops
    station.timezone = "Europe/Bucharest"
    assert ev.resolve_local_deadline(date(2026, 3, 29), "03:30", station.timezone) is None
    assert ev.resolve_local_deadline(date(2026, 10, 25), "03:30", station.timezone).hour == 1
    assert ev.resolve_local_deadline(date(2028, 2, 29), "08:00", station.timezone).day == 29
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="EV"))
    ev.create_requirement(db, user, station, connector.id, EVRequirementIn(minimum_energy_kwh=3, schedule={"weekdays": list(range(7)), "local_time": "08:00"}))
    one_off = ev.create_requirement(db, user, station, connector.id, EVRequirementIn(minimum_energy_kwh=0, deadline=now + timedelta(hours=6)))
    upcoming = ev.upcoming_requirements(db, station, connector, now)
    assert upcoming[0][0].id == one_off.id and upcoming[1][1].date() == date(2026, 10, 2)


def test_recommendation_preview_expiry_supersession_and_feedback(db, ops):
    user, station, device, plan, interval, policy, now = ops
    record = recommendations.capture_plan(db, station, plan)
    preview = recommendations.preview(db, station, record)
    recommendations.act(db, station, user, record, "apply", "Approve", preview["preview_hash"])
    assert record.status == "applied" and db.scalar(select(func.count(PlanApproval.id))) == 1
    assert recommendations.act(db, station, user, record, "apply", "Retry", preview["preview_hash"]).id == record.id
    preset = recommendations.create_preset(db, station, user, "backup")
    old_pref = control.current_versions(db, station)[1]
    assert preset.snapshot["diff"]["min_reserve_soc_percent"]["before"] == str(old_pref.min_reserve_soc_percent)
    recommendations.act(db, station, user, preset, "apply", "Backup", control.digest(preset.snapshot))
    new_pref = control.current_versions(db, station)[1]
    assert new_pref.id != old_pref.id and new_pref.min_reserve_soc_percent == 60
    assert preset.status == "awaiting_plan" and not db.scalar(select(Command.id))
    other = recommendations.create_preset(db, station, user, "economy")
    other.expires_at = now - timedelta(seconds=1)
    with pytest.raises(ValueError, match="expirata"):
        recommendations.act(db, station, user, other, "apply", "Expired", control.digest(other.snapshot))


def test_operations_routes_csrf_permissions_and_no_vehicle_data_leak(client, db, ops):
    user, station, device, plan, interval, policy, now = ops
    record = recommendations.capture_plan(db, station, plan)
    db.commit()
    reset_key("login_attempts:testclient")
    login(client, user.email, "TestPass1234")
    for path in ("control", "ev", "recommendations", f"control/plans/{plan.id}", f"recommendations/{record.id}"):
        response = client.get(f"/stations/{station.id}/{path}")
        assert response.status_code == 200, response.text
    assert client.post(f"/stations/{station.id}/control/stop").status_code == 403
    other = make_station(db, make_org(db, "Other"), user)
    db.commit()
    assert client.get(f"/stations/{other.id}/ev").status_code == 403
    assert client.get(f"/stations/{other.id}/control/plans/{plan.id}").status_code == 403
    outsider = make_user(db, email="ops-outsider@test.local")
    with pytest.raises(HTTPException):
        control.approve_plan(db, station, plan, outsider, control.preview(db, station, plan)["preview_hash"])


def test_operations_migration_rollback_retains_legacy_nulls_and_new_history(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    config, pref = control.current_versions(db, station)
    config.ev_enabled, config.ev_max_charge_power_kw, config.ev_battery_capacity_kwh = True, Decimal(0), None
    pref.ev_required_energy_kwh = Decimal(0)
    pref.ev_departure_time = datetime.min.time().replace(hour=8)
    from app.models.organization import Organization
    other = make_station(db, db.get(Organization, station.organization_id), user,
                         name="Populated EV", ev_enabled=True, ev_battery_capacity_kwh=Decimal("64.125"),
                         ev_max_charge_power_kw=Decimal("7.4"))
    populated_config, _ = control.current_versions(db, other)
    approve(db, ops)
    db.flush()
    spec = spec_from_file_location("operations_migration", Path(__file__).parents[2] / "alembic/versions/b05f7d23a6c1_energy_operations.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db.connection())))
    migration.downgrade()
    assert db.execute(text("SELECT count(*) FROM legacy_plan_approvals")).scalar_one() == 1
    values = db.execute(text("SELECT ev_max_charge_power_kw, ev_battery_capacity_kwh FROM station_config_versions WHERE id=:id"), {"id": config.id}).one()
    assert values == (Decimal(0), None)
    populated = db.execute(text("SELECT ev_max_charge_power_kw, ev_battery_capacity_kwh FROM station_config_versions WHERE id=:id"), {"id": populated_config.id}).one()
    assert populated == (Decimal("7.4"), Decimal("64.125"))
    migration.upgrade()
    assert db.scalar(select(func.count(PlanApproval.id))) == 1
    imported = db.scalar(select(EVSE).where(EVSE.station_id == station.id, EVSE.source == "legacy_settings"))
    assert imported.max_power_kw == 0 and imported.config_snapshot["ev_battery_capacity_kwh"] is None
    assert imported.capabilities == {} and not imported.vehicle_data_consent
    populated_evse = db.scalar(select(EVSE).where(EVSE.station_id == other.id, EVSE.source == "legacy_settings"))
    assert populated_evse.max_power_kw == Decimal("7.4")
    assert Decimal(str(populated_evse.config_snapshot["ev_battery_capacity_kwh"])) == Decimal("64.125")


def test_missing_readback_suspends_after_application(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    command = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    device_service.acknowledge_command(db, device, command.id, "accepted", None)
    device_service.report_command_result(db, device, command.id, "executed", {}, None)
    control.reconcile_station(db, station, now + timedelta(minutes=3))
    verification = db.scalar(select(CommandVerification).where(CommandVerification.command_id == command.id))
    assert verification.status == "read_back_timeout"
    assert control.control_state(db, station).mode == "suspended"


def test_outcome_preserves_unknown_grid_and_simulated_carry_in(db, ops):
    user, station, device, plan, interval, policy, now = ops
    raw = db.scalar(select(TelemetryRaw).where(TelemetryRaw.station_id == station.id))
    raw.is_simulated = True
    raw.grid_power_w = None
    db.add(TelemetryRaw(station_id=station.id, device_id=device.id, boot_id="ops", sequence=2,
                        measured_at=now + timedelta(minutes=4), received_at=now + timedelta(minutes=4),
                        battery_power_w=500, grid_power_w=None))
    db.flush()
    evidence = control.measured_outcome(db, interval)["metrics"]
    assert evidence["grid_kw"]["observed"] is None and evidence["grid_kw"]["coverage"] == "0"
    assert evidence["battery_kw"]["untrusted_contributor"] is True
    assert Decimal(evidence["battery_kw"]["coverage"]) < 1
    assert Decimal(evidence["battery_kw"]["observed"]) == Decimal(".5")


def test_ev_tariff_boundary_keeps_missing_prices_and_fractional_energy(db, ops):
    user, station, device, plan, interval, policy, now = ops
    tariff = Tariff(station_id=station.id, direction="import", kind="fixed", name="Boundary")
    db.add(tariff)
    db.flush()
    first = TariffVersion(tariff_id=tariff.id, valid_from=now, valid_to=now + timedelta(minutes=1), fixed_price_lei_per_kwh=Decimal(1))
    second = TariffVersion(tariff_id=tariff.id, valid_from=now + timedelta(minutes=1), fixed_price_lei_per_kwh=Decimal(2))
    db.add_all([first, second])
    db.flush()
    segments = [{"start": now, "end": now + timedelta(minutes=3), "energy_kwh": Decimal(1), "quality": "measured"}]
    cost, priced, known = analytics.cost_segments(db, station.id, segments)
    assert priced == known == Decimal(1)
    assert abs(cost - Decimal(5) / 3) < Decimal("1e-25")
    second.economic_calculation_disabled = True
    db.flush()
    partial_cost, priced, known = analytics.cost_segments(db, station.id, segments)
    assert priced < known and partial_cost == Decimal(1) / 3


def test_ev_consent_revocation_retention_and_conflicting_retries(db, ops):
    user, station, device, plan, interval, policy, now = ops
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="Private", device_id=device.id,
        vehicle_data_consent=True, capabilities={"meter": True, "vehicle_soc": True}))
    vehicle = ev.create_vehicle(db, user, station, evse, VehicleIn(alias="Car", consumption_kwh_100km=15))
    data = EVObservationIn(event_id="start", observed_at=now, state="connected", meter_kwh=0,
                            vehicle_soc_percent=0, vehicle_id=vehicle.id)
    observation, _ = ev.ingest_observation(db, device, connector.id, data, now)
    with pytest.raises(ValueError, match="reutilizata"):
        ev.ingest_observation(db, device, connector.id, data.model_copy(update={"meter_kwh": Decimal(1)}), now)
    end = now + timedelta(minutes=5)
    ev.ingest_observation(db, device, connector.id, EVObservationIn(event_id="end", observed_at=end,
                          state="completed", meter_kwh=1), end)
    session = db.get(ChargingSession, observation.session_id)
    assert session.vehicle_id == vehicle.id
    ev.update_privacy(db, user, station, evse, False, 7, now + timedelta(days=1))
    assert session.vehicle_id is None and observation.vehicle_soc_percent is None
    assert ev.upcoming_requirements(db, station, connector, now) == []
    with pytest.raises(ValueError):
        ev.create_requirement(db, user, station, connector.id, EVRequirementIn(minimum_energy_kwh=1,
                              target_soc_percent=50, deadline=now + timedelta(hours=1)))
    assert ev.retention(db, now + timedelta(days=8)) == 1
    assert db.scalar(select(func.count(EVObservation.id)).where(EVObservation.connector_id == connector.id)) == 0


def test_scenario_uses_real_solver_and_never_applies_changes(db, ops):
    user, station, device, plan, interval, policy, now = ops
    key = interval.interval_start.isoformat()
    plan.optimization_run.input_snapshot = {**plan.optimization_run.input_snapshot,
        "pv_forecast_kw": {key: 1}, "load_forecast_kw": {key: 1},
        "price_buy_lei_kwh": {key: 1}, "price_sell_lei_kwh": {key: .5}}
    result = recommendations.compare_scenarios(db, station, plan, Decimal(20), Decimal(1), "cost")
    assert result["applied"] is False and len(result["results"]) == 3
    assert all(r["status"] == "optimal" for r in result["results"])
    assert plan.execution_mode == "shadow" and db.scalar(select(func.count(PlanApproval.id))) == 0


def test_recommendation_feedback_snooze_and_stale_preview(db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    preset = recommendations.create_preset(db, station, user, "economy")
    assert recommendations.create_preset(db, station, user, "economy").id == preset.id
    recommendations.act(db, station, user, preset, "snooze", "Later", snoozed_until=now + timedelta(minutes=5))
    assert preset not in recommendations.ranked(db, station, now)
    assert preset in recommendations.ranked(db, station, now + timedelta(minutes=6))
    recommendations.act(db, station, user, preset, "not_relevant", "Wrong priority")
    assert preset.status == "not_relevant"
    record = recommendations.capture_plan(db, station, plan)
    with pytest.raises(ValueError, match="Preview"):
        recommendations.act(db, station, user, record, "apply", "Changed preview", "incorrect")
    control.suspend(db, station, "operator_stop", user)
    with pytest.raises(ValueError, match="Assisted"):
        recommendations.act(db, station, user, record, "apply", "Suspended", control.preview(db, station, plan)["preview_hash"])


def test_concurrent_approval_and_ev_retry_are_single_records(engine, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.models.device import Device
    from app.models.optimization import Plan
    from app.models.organization import Organization
    from app.models.station import Station
    from app.models.user import User
    from tests.energy_helpers import make_control_context

    with Session(engine) as setup:
        user, station, device, plan, interval, policy, now = make_control_context(setup, monkeypatch)
        evse, connector = ev.create_evse(setup, user, station, EVSEIn(name="Race", device_id=device.id, capabilities={"meter": True}))
        ids = user.id, station.id, device.id, plan.id, connector.id, station.organization_id
        preview_hash = control.preview(setup, station, plan, now)["preview_hash"]
        setup.commit()
    barrier = Barrier(2)

    def attempt():
        with Session(engine) as db:
            user, station, device, plan = [db.get(model, key) for model, key in zip((User, Station, Device, Plan), ids[:4], strict=True)]
            barrier.wait(timeout=5)
            approval = control.approve_plan(db, station, plan, user, preview_hash, now)
            _, status = ev.ingest_observation(db, device, ids[4], EVObservationIn(
                event_id="same-event", observed_at=now, state="connected", meter_kwh=0), now)
            result = approval.id, status
            db.commit()

            return result

    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        assert results[0][0] == results[1][0]
        assert sorted(r[1] for r in results) == ["accepted", "duplicate"]
        with Session(engine) as db:
            assert db.scalar(select(func.count(ChargingSession.id)).where(ChargingSession.connector_id == ids[4])) == 1
    finally:
        with Session(engine) as db:
            from app.models.audit import AuditLog
            db.execute(delete(AuditLog).where(AuditLog.station_id == ids[1]))
            db.execute(delete(Station).where(Station.id == ids[1]))
            db.execute(delete(Organization).where(Organization.id == ids[5]))
            db.execute(delete(User).where(User.id == ids[0]))
            db.commit()


@pytest.mark.parametrize("day", [date(2026, 3, 28), date(2026, 10, 24), date(2028, 2, 28)])
def test_ev_session_across_midnight_dst_and_leap_day_uses_elapsed_instants(db, ops, day):
    user, station, device, plan, interval, policy, now = ops
    zone = ZoneInfo("Europe/Bucharest")
    start = datetime.combine(day, datetime.min.time().replace(hour=23), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time().replace(hour=5), tzinfo=zone)
    # Input offsets represent instants; PostgreSQL returns their UTC equivalents.
    from datetime import UTC
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="Overnight", device_id=device.id, capabilities={"meter": True}))
    for event_id, at, state, meter in (("a", start, "connected", "10"), ("b", end, "completed", "12.125")):
        ev.ingest_observation(db, device, connector.id, EVObservationIn(event_id=event_id, observed_at=at, state=state, meter_kwh=meter), at)
    session = db.scalar(select(ChargingSession).where(ChargingSession.connector_id == connector.id))
    summary = analytics.session_summary(db, session)
    assert summary["total_energy_kwh"] == Decimal("2.125") and summary["energy_coverage"] == 1
    assert summary["estimated_cost_lei"] is None and summary["source_estimate"]["values"] is None


def test_ev_device_http_scope_and_control_capability_revocation(client, db, ops):
    user, station, device, plan, interval, policy, now = ops
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="Bound", device_id=device.id))
    from tests.factories import make_device
    other_device = make_device(db, station)
    evse.device_id = other_device.id
    db.commit()
    response = client.post(f"/api/v1/ev/connectors/{connector.id}/observations",
        headers={"Authorization": f"Bearer {device.id}.ops-secret"},
        json={"event_id": "wrong-device", "observed_at": now.isoformat(), "state": "connected"})
    assert response.status_code == 409
    assert db.scalar(select(func.count(EVObservation.id)).where(EVObservation.connector_id == connector.id)) == 0
    device_service.record_heartbeat(db, device, boot_id="new-heartbeat", firmware_version="test-agent", capabilities={})
    assert device.capabilities["inverter_write"] is False and "closed_loop" not in device.capabilities
    assert "read_only_device" in control.preview(db, station, plan, now)["reasons"]


def test_populated_ev_pages_and_csv_preserve_unknown_cost(client, db, ops):
    user, station, device, plan, interval, policy, now = ops
    evse, connector = ev.create_evse(db, user, station, EVSEIn(name="Export EV", device_id=device.id, capabilities={"meter": True}))
    for event, at, state, meter in (("start", now, "connected", 0), ("end", now + timedelta(minutes=15), "completed", 1)):
        ev.ingest_observation(db, device, connector.id, EVObservationIn(event_id=event, observed_at=at, state=state, meter_kwh=meter), at)
    session = db.scalar(select(ChargingSession).where(ChargingSession.connector_id == connector.id))
    db.commit()
    reset_key("login_attempts:testclient")
    login(client, user.email, "TestPass1234")
    for path in ("ev", f"ev/sessions/{session.id}"):
        response = client.get(f"/stations/{station.id}/{path}")
        assert response.status_code == 200 and "Necunoscut" in response.text
    response = client.get(f"/stations/{station.id}/ev/export.csv", params={"start": now.isoformat(), "end": (now + timedelta(days=1)).isoformat()})
    import csv
    import io
    record = next(csv.DictReader(io.StringIO(response.text)))
    assert Decimal(record["energy_kwh"]) == 1 and record["estimated_grid_equivalent_cost_lei"] == ""
    assert record["quality"] == "measured" and Decimal(record["coverage"]) == 1
