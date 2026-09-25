from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.api.deps import StationAccess
from app.config import get_settings
from app.core.security import utcnow
from app.models.command import Command, CommandEvent
from app.models.control import (
    CommandVerification,
    ControlPolicy,
    PlanApproval,
    PlanOutcome,
    StationControl,
)
from app.models.device import Device
from app.models.optimization import Plan, PlanInterval
from app.models.organization import Organization
from app.models.preference import PreferenceVersion
from app.models.station import StationConfigVersion
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.schemas.energy_operations import ControlPolicyIn, ReadbackIn
from app.services.ev_analytics_service import seconds
from app.services.ev_service import audit, lock_station

ZERO = Decimal(0)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def decimal_text(value):
    if value is None:
        return None
    number = Decimal(str(value))
    return "0" if number == 0 else format(number.normalize(), "f")


def current_versions(db, station):
    config = db.scalar(select(StationConfigVersion).where(StationConfigVersion.station_id == station.id)
                       .order_by(StationConfigVersion.version.desc()).limit(1))
    preference = db.scalar(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id)
                           .order_by(PreferenceVersion.version.desc()).limit(1))
    return config, preference


def control_state(db, station, create=False):
    state = db.scalar(select(StationControl).where(StationControl.station_id == station.id))
    if state is None and create:
        lock_station(db, station.id)
        state = db.scalar(select(StationControl).where(StationControl.station_id == station.id))
        if state is None:
            state = StationControl(station_id=station.id)
            db.add(state)
            db.flush()
    return state


def save_policy(db, station, user, data: ControlPolicyIn):
    StationAccess("organization_admin")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    now = utcnow()
    if not now < data.expires_at <= now + timedelta(days=30):
        raise ValueError("Politica trebuie sa expire in cel mult 30 zile.")
    device = db.get(Device, data.device_id)
    if not device or device.station_id != station.id or device.status != "active":
        raise ValueError("Dispozitiv neautorizat pentru statie.")
    version = (db.scalar(select(func.max(ControlPolicy.version)).where(ControlPolicy.station_id == station.id)) or 0) + 1
    policy = ControlPolicy(station_id=station.id, version=version, limits=data.model_dump(mode="json"), created_by=user.id)
    db.add(policy)
    db.flush()
    state = control_state(db, station, True)
    state.policy_id = policy.id
    suspend(db, station, "policy_changed", user)
    audit(db, station, user, "control.policy_saved", policy.id, {"version": version})
    return policy


def device_gate(device, station, now):
    settings = get_settings()
    if not settings.closed_loop_execution_enabled:
        return "execution_feature_disabled"
    if not device or device.station_id != station.id or device.status != "active":
        return "device_unavailable"
    if not device.last_heartbeat_at or not timedelta(0) <= now - device.last_heartbeat_at <= timedelta(minutes=5):
        return "device_offline"
    caps = device.capabilities or {}
    contract = caps.get("closed_loop", {})
    if not isinstance(contract, dict) or caps.get("inverter_write") is not True or contract.get("schema_version") != 1:
        return "read_only_device"
    if (contract.get("readback") is not True or not isinstance(contract.get("commands"), list) or
            "set_battery_target_soc" not in contract["commands"]):
        return "capability_missing"
    if not all(isinstance(contract.get(key), str) for key in ("profile_id", "model", "inverter_firmware")):
        return "model_firmware_mismatch"
    profile = settings.closed_loop_verified_profiles.get(contract.get("profile_id", ""))
    if not isinstance(profile, dict) or not profile.get("hardware_verified"):
        return "hardware_profile_unverified"
    if (profile.get("model") != contract.get("model") or
            device.firmware_version not in profile.get("agent_versions", []) or
            contract.get("inverter_firmware") not in profile.get("inverter_firmware_versions", [])):
        return "model_firmware_mismatch"
    return None


def change_mode(db, station, user, mode, reason, expected_revision, confirmed):
    StationAccess("operator")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    state = control_state(db, station, True)
    if mode not in ("shadow", "assisted", "automatic", "suspended") or not reason.strip() or len(reason) > 500 or not confirmed:
        raise ValueError("Modul, confirmarea si motivul sunt obligatorii.")
    if state.revision != expected_revision:
        raise ValueError("Modul a fost modificat; reincarcati pagina.")
    if mode == "automatic" and not get_settings().closed_loop_automatic_enabled:
        raise ValueError("Modul Automatic necesita activare dupa validarea hardware.")
    if mode in ("assisted", "automatic"):
        policy = db.get(ControlPolicy, state.policy_id) if state.policy_id else None
        if not policy or datetime.fromisoformat(policy.limits["expires_at"]) <= utcnow():
            raise ValueError("Este necesara o politica valida.")
        device = db.get(Device, policy.limits["device_id"])
        reason_code = device_gate(device, station, utcnow())
        if reason_code:
            raise ValueError(reason_code)
    state.mode, state.reason, state.changed_by = mode, reason.strip(), user.id
    state.revision += 1
    station.execution_mode = "live" if mode in ("assisted", "automatic") else "shadow"
    cancel_pending(db, station, "mode_changed")
    audit(db, station, user, "control.mode_changed", state.id, {"mode": mode, "revision": state.revision, "reason": reason.strip()})
    db.flush()
    return state


def cancel_pending(db, station, reason):
    for command in db.scalars(select(Command).where(
        Command.station_id == station.id, Command.plan_interval_id.is_not(None),
        Command.status.in_(("created", "delivered", "accepted")),
    ).with_for_update()):
        command.status = "superseded"
        db.add(CommandEvent(command_id=command.id, event_type="superseded", source="system", message=reason))


def suspend(db, station, reason, user=None):
    state = control_state(db, station, True)
    if state.mode != "suspended" or state.reason != reason:
        state.mode, state.reason, state.changed_by = "suspended", reason, user.id if user else None
        state.revision += 1
        station.execution_mode = "shadow"
        cancel_pending(db, station, reason)
        audit(db, station, user, "control.suspended", state.id, {"reason": reason, "physical_stop_confirmed": False})
    return state


def plan_snapshot(db, station, plan, policy):
    # PostgreSQL applies Numeric precision on write. Freeze those persisted
    # values so retries from a fresh worker produce the same approval hash.
    db.flush()
    db.refresh(plan.optimization_run, ["objective_value_lei"])
    config, preference = current_versions(db, station)
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)
                           .order_by(PlanInterval.interval_start).execution_options(populate_existing=True)).all()
    return {
        "schema_version": 1, "plan_id": str(plan.id), "plan_version": plan.version,
        "config_id": str(config.id) if config else None, "preference_id": str(preference.id) if preference else None,
        "policy": policy.limits if policy else None, "policy_id": str(policy.id) if policy else None,
        "input": plan.optimization_run.input_snapshot,
        "objective_lei": decimal_text(plan.optimization_run.objective_value_lei),
        "explanation": plan.optimization_run.explanation_summary,
        "intervals": [{"id": str(i.id), "start": i.interval_start.isoformat(), "end": i.interval_end.isoformat(),
                       "battery_kw": decimal_text(i.battery_power_target_kw), "soc_percent": decimal_text(i.battery_soc_target_percent),
                       "grid_kw": decimal_text(i.grid_power_target_kw), "ev_kw": decimal_text(i.ev_charge_power_kw),
                       "pv_kw": decimal_text(i.pv_forecast_kw), "load_kw": decimal_text(i.load_forecast_kw),
                       "import_lei_kwh": decimal_text(i.price_import_lei_kwh),
                       "export_lei_kwh": decimal_text(i.price_export_lei_kwh), "explanation": i.explanation}
                      for i in intervals],
    }


def historical_discharge(db, station, start, end):
    if end <= start:
        return ZERO
    rows = db.scalars(select(TelemetryAggregate).where(
        TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_type == "interval_15m",
        TelemetryAggregate.period_start >= start, TelemetryAggregate.period_end <= end,
    ).order_by(TelemetryAggregate.period_start)).all()
    energy, covered = ZERO, ZERO
    for row in rows:
        if row.battery_discharge_energy_kwh is None or row.data_quality != "measured":
            continue
        energy += row.battery_discharge_energy_kwh
        covered += seconds(row.period_end - row.period_start) * Decimal(str(row.coverage.get("battery", 0)))
    # A missing segment is not free cycle budget. The current partial interval
    # is bounded conservatively by the physical discharge limit in policy_check.
    return energy if covered >= seconds(end - start) * Decimal("0.99") else None


def policy_check(db, station, plan, policy, now=None, *, device_required=True):
    now = now or utcnow()
    reasons = []
    org = db.get(Organization, station.organization_id)
    if not station.is_active or not org or org.status != "active":
        reasons.append("station_inactive")
    if not policy or policy.station_id != station.id:
        return ["policy_missing"]
    limits = ControlPolicyIn.model_validate(policy.limits)
    if limits.expires_at <= now:
        reasons.append("policy_expired")
    device = db.scalar(select(Device).where(Device.id == limits.device_id).execution_options(populate_existing=True))
    if device_required and (gate := device_gate(device, station, now)):
        reasons.append(gate)
    run = plan.optimization_run
    config, pref = current_versions(db, station)
    if not config or not pref or run.station_config_version_id != config.id or run.preference_version_id != pref.id:
        return [*reasons, "configuration_superseded"]
    if plan.status not in ("published", "accepted_by_device", "executing") or run.is_fallback or run.status != "succeeded":
        reasons.append("plan_unavailable")
    if pref.automation_suspended_until and pref.automation_suspended_until > now:
        reasons.append("manual_override")
    snapshot = run.input_snapshot
    if snapshot.get("soc", {}).get("quality") != "measured" or any(
        not snapshot.get(key) or any(value != "measured" for value in snapshot[key].values())
        for key in ("pv_forecast_quality", "load_forecast_quality", "price_buy_quality")
    ):
        reasons.append("untrusted_plan_inputs")
    latest = db.scalar(select(TelemetryRaw).where(TelemetryRaw.station_id == station.id)
                       .order_by(TelemetryRaw.measured_at.desc()).limit(1))
    if (not latest or latest.battery_soc_percent is None or latest.battery_power_w is None or
            latest.is_simulated or any(latest.quality_flags.get(k) for k in ("stale", "simulated", "derived")) or
            not timedelta(0) <= now - latest.measured_at <= timedelta(minutes=5)):
        reasons.append("fresh_soc_power_required")
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)
                           .order_by(PlanInterval.interval_start)).all()
    remaining = [i for i in intervals if i.interval_end > now]
    if not remaining or remaining[-1].interval_end > limits.expires_at:
        return [*reasons, "plan_expired_or_outside_policy"]
    technical = (config.battery_max_charge_power_kw, config.battery_max_discharge_power_kw,
                 config.battery_available_capacity_kwh, config.battery_reference_capacity_kwh,
                 config.grid_import_limit_kw, config.grid_export_limit_kw)
    if any(value is None for value in technical) or config.battery_reference_capacity_kwh <= 0:
        return [*reasons, "technical_limits_unknown"]
    low, high = max(limits.min_soc_percent, pref.min_reserve_soc_percent), min(limits.max_soc_percent, pref.max_normal_soc_percent)
    if latest and latest.battery_soc_percent is not None and not low <= latest.battery_soc_percent <= high:
        reasons.append("current_soc_outside_policy")
    charge_limit = min(limits.max_charge_kw, config.battery_max_charge_power_kw)
    discharge_limit = min(limits.max_discharge_kw, config.battery_max_discharge_power_kw)
    managed = limits.max_managed_energy_kwh
    if pref.max_optimization_energy_kwh is not None:
        managed = min(managed, pref.max_optimization_energy_kwh)
    energy, day_energy, month_energy = ZERO, {}, {}
    previous_power = Decimal(str(latest.battery_power_w)) / 1000 if latest and latest.battery_power_w is not None else ZERO
    previous_time = now
    zone = ZoneInfo(station.timezone)
    for interval in remaining:
        power, soc = interval.battery_power_target_kw, interval.battery_soc_target_percent
        values = (power, soc, interval.grid_power_target_kw, interval.ev_charge_power_kw, interval.pv_forecast_kw, interval.load_forecast_kw)
        if any(v is None or not Decimal(str(v)).is_finite() for v in values):
            reasons.append("nonfinite_target")
            continue
        if not -discharge_limit <= power <= charge_limit or not low <= soc <= high:
            reasons.append("battery_limit")
        if not -config.grid_export_limit_kw <= interval.grid_power_target_kw <= config.grid_import_limit_kw:
            reasons.append("grid_limit")
        if interval.ev_charge_power_kw != 0:
            reasons.append("ev_executor_unverified")
        if power > max(interval.pv_forecast_kw, ZERO) and not pref.allow_grid_charge:
            reasons.append("grid_charge_forbidden")
        if -power > interval.load_forecast_kw and not pref.allow_battery_export:
            reasons.append("battery_export_forbidden")
        minutes = max(seconds(max(interval.interval_start, now) - previous_time) / 60, Decimal(1))
        if abs(power - previous_power) > limits.max_ramp_kw_per_minute * minutes:
            reasons.append("ramp_limit")
        previous_power, previous_time = power, interval.interval_start
        cursor, end = max(interval.interval_start, now), interval.interval_end
        energy += abs(power) * seconds(end - cursor) / 3600
        while cursor < end:
            local = cursor.astimezone(zone)
            minute_end = min(end, cursor + timedelta(minutes=1))
            minute = local.hour * 60 + local.minute
            if power != 0 and not any(local.weekday() in w.days and w.start_minute <= minute < w.end_minute for w in limits.windows):
                reasons.append("outside_operating_window")
            discharge = max(-power, ZERO) * seconds(minute_end - cursor) / 3600
            day_energy[local.date()] = day_energy.get(local.date(), ZERO) + discharge
            month = local.date().replace(day=1)
            month_energy[month] = month_energy.get(month, ZERO) + discharge
            cursor = minute_end
    if energy > managed:
        reasons.append("managed_energy_limit")
    boundary = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    for groups, maximum, preference_limit in (
        (day_energy, limits.max_efc_day, pref.max_efc_per_day),
        (month_energy, limits.max_efc_month, pref.max_efc_per_month),
    ):
        budget = min(maximum, preference_limit) if preference_limit is not None else maximum
        for day, discharge in groups.items():
            start = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(UTC)
            used = historical_discharge(db, station, start, min(boundary, now))
            if used is None:
                reasons.append("efc_history_unknown")
            else:
                partial = config.battery_max_discharge_power_kw * max(seconds(now - max(start, boundary)), ZERO) / 3600
                if used + partial + discharge > budget * config.battery_reference_capacity_kwh:
                    reasons.append("efc_budget")
    return sorted(set(reasons))


def preview(db, station, plan, now=None):
    state = control_state(db, station)
    policy = db.get(ControlPolicy, state.policy_id) if state and state.policy_id else None
    snapshot = plan_snapshot(db, station, plan, policy)
    latest = db.scalar(select(TelemetryRaw).where(TelemetryRaw.station_id == station.id)
                       .order_by(TelemetryRaw.measured_at.desc()).limit(1))
    commands = db.execute(select(Command, CommandVerification).outerjoin(
        CommandVerification, CommandVerification.command_id == Command.id,
    ).join(PlanInterval, PlanInterval.id == Command.plan_interval_id).where(PlanInterval.plan_id == plan.id)
      .order_by(Command.valid_from)).all()
    outcomes = db.scalars(select(PlanOutcome).join(PlanInterval, PlanInterval.id == PlanOutcome.interval_id)
                         .where(PlanInterval.plan_id == plan.id)).all()
    return {"snapshot": snapshot, "preview_hash": digest(snapshot),
            "reasons": policy_check(db, station, plan, policy, now),
            "mode": state.mode if state else "shadow", "revision": state.revision if state else 0,
            "observed": latest, "commands": commands, "outcomes": outcomes}


def approve_plan(db, station, plan, user, preview_hash, now=None, *, automatic=False):
    now = now or utcnow()
    if not automatic:
        StationAccess("operator")(station_id=station.id, db=db, user=user)
    lock_station(db, station.id)
    if plan.station_id != station.id:
        raise ValueError("Plan neautorizat.")
    state = control_state(db, station)
    expected_mode = "automatic" if automatic else "assisted"
    if not state or state.mode != expected_mode or (automatic and not get_settings().closed_loop_automatic_enabled):
        raise ValueError("Aprobarea necesita modul Assisted sau o politica Automatic activata explicit.")
    policy = db.get(ControlPolicy, state.policy_id)
    snapshot = plan_snapshot(db, station, plan, policy)
    actual_hash = digest(snapshot)
    existing = db.scalar(select(PlanApproval).where(PlanApproval.plan_id == plan.id))
    if existing:
        if existing.snapshot_hash == preview_hash == actual_hash and existing.control_revision == state.revision:
            return existing
        raise ValueError("Aprobarea existenta nu poate fi modificata; generati un plan nou.")
    if actual_hash != preview_hash:
        raise ValueError("Preview depasit; verificati din nou diferentele.")
    reasons = policy_check(db, station, plan, policy, now)
    if reasons:
        raise ValueError(", ".join(reasons))
    approval = PlanApproval(
        station_id=station.id, plan_id=plan.id, device_id=ControlPolicyIn.model_validate(policy.limits).device_id, policy_id=policy.id,
        control_revision=state.revision, approved_by=user.id if user else None, mode=state.mode,
        snapshot=snapshot, snapshot_hash=actual_hash,
        expires_at=min(datetime.fromisoformat(policy.limits["expires_at"]), plan.optimization_run.horizon_end),
    )
    db.add(approval)
    plan.execution_mode = "live"
    audit(db, station, user, "control.plan_approved", plan.id, {"snapshot_hash": actual_hash, "mode": state.mode})
    db.flush()
    return approval


def execution_authorized(db, station, plan, device, now):
    state = control_state(db, station)
    approval = db.scalar(select(PlanApproval).where(PlanApproval.plan_id == plan.id))
    if not state or not approval or state.mode not in ("assisted", "automatic"):
        return False
    if (approval.control_revision != state.revision or approval.mode != state.mode or
            approval.device_id != device.id or approval.expires_at <= now or approval.policy_id != state.policy_id):
        return False
    if state.mode == "automatic" and not get_settings().closed_loop_automatic_enabled:
        return False
    policy = db.get(ControlPolicy, approval.policy_id)
    if digest(plan_snapshot(db, station, plan, policy)) != approval.snapshot_hash:
        return False
    return not policy_check(db, station, plan, policy, now)


def record_application(db, command):
    if command.plan_interval_id is None:
        return
    verification = db.scalar(select(CommandVerification).where(CommandVerification.command_id == command.id))
    if verification is None:
        verification = CommandVerification(command_id=command.id)
        db.add(verification)
    if command.status == "executed":
        verification.applied_at, verification.status = command.executed_at, "applied"
    elif command.status in ("failed", "rejected", "expired"):
        verification.status, verification.reason = "failed", command.status
        station = lock_station(db, command.station_id)
        suspend(db, station, "partial_execution_failure")
    db.flush()


def verify_readback(db, device, command_id, data: ReadbackIn, now=None):
    now = now or utcnow()
    station = lock_station(db, device.station_id)
    command = db.scalar(select(Command).where(Command.id == command_id).with_for_update())
    if not station or not command or command.device_id != device.id or command.station_id != station.id or not command.plan_interval_id:
        raise ValueError("Comanda neautorizata pentru read-back.")
    verification = db.scalar(select(CommandVerification).where(CommandVerification.command_id == command.id))
    payload = data.model_dump(mode="json")
    if verification and verification.read_back_at:
        if verification.evidence == payload:
            return verification
        raise ValueError("Read-back contradictoriu pentru o verificare imutabila.")
    if command.status != "executed" or not verification or not verification.applied_at:
        raise ValueError("Aplicarea trebuie raportata separat inainte de read-back.")
    if (data.command_version != command.version or data.idempotency_key != command.idempotency_key or
            not max(command.valid_from, verification.applied_at) <= data.observed_at < command.expires_at or
            not timedelta(0) <= now - data.observed_at <= timedelta(minutes=5)):
        raise ValueError("Read-back expirat sau cu identitate diferita.")
    if device_gate(device, station, now):
        raise ValueError("Capabilitatea de read-back nu mai este valida.")
    matches = (
        abs(data.target_soc_percent - Decimal(str(command.parameters["target_soc_percent"]))) <= Decimal(".01") and
        abs(data.battery_power_kw - Decimal(str(command.parameters["battery_power_kw"]))) <= Decimal(".01")
    )
    verification.read_back_at, verification.evidence = data.observed_at, payload
    verification.status = "read_back_verified" if matches else "read_back_mismatch"
    verification.reason = None if matches else "target_mismatch"
    if not matches:
        suspend(db, station, "read_back_mismatch")
    audit(db, station, None, "control.readback", command.id, {"status": verification.status})
    db.flush()
    return verification


def measured_outcome(db, interval):
    plan = db.get(Plan, interval.plan_id)
    rows = db.scalars(select(TelemetryRaw).where(
        TelemetryRaw.station_id == plan.station_id,
        TelemetryRaw.measured_at >= interval.interval_start - timedelta(minutes=5),
        TelemetryRaw.measured_at < interval.interval_end,
    ).order_by(TelemetryRaw.measured_at)).all()
    evidence = {"start": interval.interval_start.isoformat(), "end": interval.interval_end.isoformat(),
                "method": "time_weighted_measured_power_v1", "metrics": {}}
    duration = seconds(interval.interval_end - interval.interval_start)
    for metric, field, divisor, expected in (
        ("battery_kw", "battery_power_w", Decimal(1000), interval.battery_power_target_kw),
        ("grid_kw", "grid_power_w", Decimal(1000), interval.grid_power_target_kw),
    ):
        total, covered, tainted = ZERO, ZERO, False
        samples = [row for row in rows if getattr(row, field) is not None]
        for index, row in enumerate(samples):
            start = max(row.measured_at, interval.interval_start)
            end = min(row.measured_at + timedelta(minutes=5), interval.interval_end,
                      samples[index + 1].measured_at if index + 1 < len(samples) else interval.interval_end)
            if end <= start:
                continue
            if row.is_simulated or any(row.quality_flags.get(k) for k in ("simulated", "stale", "derived")):
                tainted = True
                continue
            covered += seconds(end - start)
            total += getattr(row, field) / divisor * seconds(end - start)
        value = total / covered if covered > 0 else None
        evidence["metrics"][metric] = {
            "expected": str(expected), "observed": str(value) if value is not None else None,
            "coverage": str(covered / duration), "untrusted_contributor": tainted,
            "deviation": str(value - expected) if value is not None else None,
        }
    return evidence


def reconcile_station(db, station, now=None):
    now = now or utcnow()
    lock_station(db, station.id)
    state = control_state(db, station)
    if state and state.mode == "automatic" and get_settings().closed_loop_automatic_enabled:
        plan = db.scalar(select(Plan).where(
            Plan.station_id == station.id, Plan.status.in_(("published", "accepted_by_device", "executing")),
        ).order_by(Plan.version.desc()).limit(1))
        if plan and not db.scalar(select(PlanApproval.id).where(PlanApproval.plan_id == plan.id)):
            policy = db.get(ControlPolicy, state.policy_id) if state.policy_id else None
            if policy and not policy_check(db, station, plan, policy, now):
                approve_plan(db, station, plan, None, digest(plan_snapshot(db, station, plan, policy)), now, automatic=True)
    commands = db.scalars(select(Command).where(
        Command.station_id == station.id, Command.plan_interval_id.is_not(None),
        Command.expires_at > now - timedelta(days=2),
    ).order_by(Command.valid_from)).all()
    for command in commands:
        verification = db.scalar(select(CommandVerification).where(CommandVerification.command_id == command.id))
        if command.status in ("created", "delivered", "accepted") and command.expires_at <= now:
            command.status = "expired"
            db.add(CommandEvent(command_id=command.id, event_type="expired", source="system", message="execution_timeout"))
            record_application(db, command)
        elif command.status in ("failed", "rejected", "expired") and (not verification or verification.status != "failed"):
            record_application(db, command)
        elif command.status == "executed" and verification and not verification.read_back_at and command.executed_at + timedelta(minutes=2) <= now:
            verification.status, verification.reason = "read_back_timeout", "read_back_missing"
            suspend(db, station, "read_back_timeout")
        interval = db.get(PlanInterval, command.plan_interval_id)
        if interval.interval_end > now - timedelta(minutes=5) or db.scalar(select(PlanOutcome.id).where(PlanOutcome.interval_id == interval.id)):
            continue
        evidence = measured_outcome(db, interval)
        complete = all(Decimal(m["coverage"]) >= Decimal(".9") and not m["untrusted_contributor"] for m in evidence["metrics"].values())
        deviation = complete and any(abs(Decimal(m["deviation"])) > Decimal(".5") for m in evidence["metrics"].values())
        verified = verification and verification.status == "read_back_verified"
        status = "outcome_verified" if complete and verified and not deviation else (
            "deviation" if deviation else "partial" if not complete else "application_unverified"
        )
        evidence["reason"] = {"outcome_verified": "within_0.5kw_tolerance", "deviation": "measured_power_differs",
                              "partial": "missing_or_untrusted_measurements", "application_unverified": "no_verified_readback"}[status]
        if status == "partial" and now < interval.interval_end + timedelta(days=1):
            continue
        db.add(PlanOutcome(interval_id=interval.id, status=status, evidence=evidence))
        interval.observed_battery_power_kw = Decimal(evidence["metrics"]["battery_kw"]["observed"]) if evidence["metrics"]["battery_kw"]["observed"] is not None else None
        interval.observed_grid_power_kw = Decimal(evidence["metrics"]["grid_kw"]["observed"]) if evidence["metrics"]["grid_kw"]["observed"] is not None else None
        interval.deviation_notes = evidence["reason"]
        audit(db, station, None, "control.outcome", interval.id, {"status": status})
    db.flush()
