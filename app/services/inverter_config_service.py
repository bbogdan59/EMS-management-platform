from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.core.audit import record_audit
from app.core.security import utcnow
from app.models.command import Command, CommandEvent
from app.models.device import Device
from app.models.inverter_config import InverterDesired, InverterProfile, InverterReport
from app.models.station import Station
from app.schemas.inverter_config import Profile

COMMAND_TYPE = 'apply_inverter_settings'


class ConfigConflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def latest(db, model, device_id):
    return db.scalar(select(model).where(model.device_id == device_id).order_by(model.version.desc()).limit(1))


def lock_device(db, device_id, station_id=None):
    device = db.scalar(select(Device).where(Device.id == device_id).with_for_update().execution_options(populate_existing=True))
    if device is None or (station_id is not None and device.station_id != station_id) or device.status != 'active':
        raise ConfigConflict('Device unavailable for this station')
    return device


def approved_profile(db, payload, user):
    definition = payload.model_dump(mode='json')
    hashed = digest(definition)
    # The caller is a platform admin; approval is an explicit engineering action.
    existing = db.scalar(select(InverterProfile).where(InverterProfile.digest == hashed))
    if existing:
        return existing
    row = InverterProfile(definition=definition, digest=hashed, approved_by=user.id)
    db.add(row)
    db.flush()
    record_audit(db, action='inverter_profile_approved', resource_type='inverter_profile',
                 resource_id=str(row.id), actor_user_id=user.id, metadata={'hash': hashed})
    return row


def checked_profile(db, profile_id):
    profile = db.get(InverterProfile, profile_id)
    if profile is None or digest(profile.definition) != profile.digest:
        raise ConfigConflict('Profile absent or integrity mismatch')
    return profile, Profile.model_validate(profile.definition)


def values_for(profile, values, *, desired):
    normalized = {}
    for key, value in values.items():
        register = profile.registers.get(key)
        if register is None or (desired and not register.writable):
            raise ConfigConflict('Setting not allowlisted for this operation: ' + key)
        if value is None:
            normalized[key] = None
            continue
        if not value.is_finite():
            raise ConfigConflict('Non-finite value')
        if desired:
            if not register.minimum <= value <= register.maximum:
                raise ConfigConflict('Setting exceeds approved limits: ' + key)
            raw = value / register.scale
            bits = 32 if register.encoding.endswith('32') else 16
            signed = register.encoding.startswith('s')
            lower, upper = (-(2 ** (bits-1)), 2 ** (bits-1)-1) if signed else (0, 2 ** bits-1)
            if raw != raw.to_integral_value() or not lower <= raw <= upper:
                raise ConfigConflict('Setting cannot be represented by register: ' + key)
        normalized[key] = format(value.normalize(), 'f')
    return normalized


def supports_write(device, station):
    return bool(station.is_active and station.execution_mode == 'live' and not station.is_demo
                and device.status == 'active'
                and (device.capabilities or {}).get('inverter_write') is True
                and (device.capabilities or {}).get('inverter_settings_v1') is True
                and device.last_heartbeat_at and device.last_heartbeat_at >= utcnow() - timedelta(minutes=5))


def save_desired(db, station, device_id, payload, user):
    device = lock_device(db, device_id, station.id)
    request_hash = digest(payload.model_dump(mode='json'))
    previous_request = db.scalar(select(InverterDesired).where(
        InverterDesired.device_id == device.id, InverterDesired.request_id == payload.request_id))
    if previous_request:
        if previous_request.request_hash != request_hash:
            raise ConfigConflict('Idempotency key reused with different content')
        return previous_request
    previous = latest(db, InverterDesired, device.id)
    if payload.expected_version != (previous.version if previous else 0):
        raise ConfigConflict('Configuration changed; reload before saving')
    profile, definition = checked_profile(db, payload.profile_id)
    settings = values_for(definition, payload.settings, desired=True)
    report = latest(db, InverterReport, device.id)
    if settings and (report is None or report.profile_id != profile.id
                     or report.model != definition.model or report.firmware not in definition.firmware
                     or report.measured_at < utcnow() - timedelta(minutes=5)):
        raise ConfigConflict('A fresh initial snapshot matching the profile is required')
    connection = payload.connection.model_dump(mode='json')
    content_hash = digest({'profile_hash': profile.digest, 'connection': connection, 'settings': settings})
    row = InverterDesired(device_id=device.id, profile_id=profile.id, version=payload.expected_version+1,
                          request_id=payload.request_id, request_hash=request_hash, digest=content_hash,
                          connection=connection, settings=settings, reason=payload.reason, created_by=user.id)
    db.add(row)
    db.flush()
    # Supersede queued configurations while holding the device lock. Already accepted
    # commands are also invalidated; the device must revalidate immediately before write.
    for old in db.scalars(select(Command).where(Command.device_id == device.id, Command.type == COMMAND_TYPE,
                                               Command.status.in_(['created', 'delivered', 'accepted']))).all():
        old.status = 'superseded'
        db.add(CommandEvent(command_id=old.id, event_type='superseded', source='system'))
    if settings and supports_write(device, station):
        now = utcnow()
        command = Command(device_id=device.id, station_id=station.id, type=COMMAND_TYPE,
                          parameters={'desired_version': row.version, 'desired_hash': row.digest,
                                      'expected_report_version': report.version, 'profile_hash': profile.digest,
                                      'settings': settings}, version=row.version,
                          idempotency_key=f'inverter-config:{row.version}', status='created', author='user',
                          author_user_id=user.id, reason=payload.reason,
                          valid_from=now, expires_at=now+timedelta(seconds=payload.ttl_seconds))
        db.add(command)
        db.flush()
        row.command_id = command.id
        db.add(CommandEvent(command_id=command.id, event_type='created', source='user'))
    record_audit(db, action='inverter_desired_created', resource_type='inverter_desired', resource_id=str(row.id),
                 actor_user_id=user.id, station_id=station.id,
                 metadata={'version': row.version, 'hash': row.digest, 'reason': payload.reason})
    db.flush()
    return row


def report_snapshot(db, device_id, payload):
    device = lock_device(db, device_id)
    request_hash = digest(payload.model_dump(mode='json'))
    duplicate = db.scalar(select(InverterReport).where(InverterReport.device_id == device.id,
                                                      InverterReport.request_id == payload.request_id))
    if duplicate:
        if duplicate.request_hash != request_hash:
            raise ConfigConflict('Report idempotency key reused with different content')
        return duplicate
    previous = latest(db, InverterReport, device.id)
    if payload.base_version != (previous.version if previous else 0):
        raise ConfigConflict('Stale report base version')
    desired = latest(db, InverterDesired, device.id)
    if desired is None or payload.desired_version != desired.version:
        raise ConfigConflict('Report must reference the current desired configuration')
    profile, definition = checked_profile(db, desired.profile_id)
    if profile.digest != payload.profile_hash or definition.model != payload.model or payload.firmware not in definition.firmware:
        raise ConfigConflict('Model, firmware or profile hash mismatch')
    if payload.measured_at > utcnow()+timedelta(minutes=2) or (previous and payload.measured_at < previous.measured_at):
        raise ConfigConflict('Report timestamp is future or older than last observation')
    if payload.kind == 'delta' and (previous is None or previous.profile_id != profile.id):
        raise ConfigConflict('Initial snapshot required before delta')
    snapshot = dict(previous.snapshot) if payload.kind == 'delta' else {}
    snapshot.update(values_for(definition, payload.settings, desired=False))
    row = InverterReport(device_id=device.id, profile_id=profile.id, request_id=payload.request_id,
                         request_hash=request_hash, version=payload.base_version+1, desired_version=desired.version,
                         snapshot=snapshot, digest=digest(snapshot), measured_at=payload.measured_at,
                         outcome=payload.outcome, reason=payload.reason, model=payload.model, firmware=payload.firmware)
    db.add(row)
    db.flush()
    record_audit(db, action='inverter_observed', resource_type='inverter_report', resource_id=str(row.id),
                 actor_label=f'device:{device.id}', station_id=device.station_id,
                 metadata={'version': row.version, 'hash': row.digest, 'outcome': row.outcome})
    return row


def reconciliation(db, device_id):
    desired = latest(db, InverterDesired, device_id)
    report = latest(db, InverterReport, device_id)
    status = 'unconfigured' if desired is None else 'pending'
    if desired and report and report.profile_id == desired.profile_id:
        if report.measured_at < utcnow()-timedelta(minutes=5):
            status = 'stale'
        elif report.desired_version == desired.version and report.outcome in ('rejected', 'failed'):
            status = report.outcome
        elif report.desired_version == desired.version and desired.settings:
            status = 'applied' if all(report.snapshot.get(k) == v for k, v in desired.settings.items()) else 'drift'
    if desired and desired.command_id and status != 'applied':
        command = db.get(Command, desired.command_id)
        if command and command.status in ('rejected', 'failed', 'expired', 'superseded'):
            status = command.status
    return desired, report, status


def delivery_allowed(db, command, device, now):
    station = db.get(Station, device.station_id)
    desired = latest(db, InverterDesired, device.id)
    report = latest(db, InverterReport, device.id)
    return bool(station and supports_write(device, station) and command.station_id == device.station_id
                and desired and desired.command_id == command.id and report
                and report.version == command.parameters.get('expected_report_version')
                and report.measured_at >= now-timedelta(minutes=5)
                and command.valid_from <= now < command.expires_at)


def validate_execution_readback(db, command, device, details):
    import uuid

    try:
        report_id = uuid.UUID(str(details.get('report_id', '')))
    except ValueError as exc:
        raise ConfigConflict('Executed result requires a stored readback report_id') from exc
    report = db.get(InverterReport, report_id)
    desired = latest(db, InverterDesired, device.id)
    if not report or not desired or report.device_id != device.id or desired.command_id != command.id:
        raise ConfigConflict('Readback does not belong to current device/configuration')
    if (report.desired_version != desired.version or report.profile_id != desired.profile_id
            or report.outcome != 'observed' or report.measured_at < (command.accepted_at or command.valid_from)
            or report.measured_at > command.expires_at
            or not all(report.snapshot.get(k) == v for k, v in desired.settings.items())):
        raise ConfigConflict('Readback does not confirm requested settings within validity window')
