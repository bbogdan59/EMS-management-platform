from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import (
    expires_in,
    generate_claim_code,
    generate_opaque_token,
    hash_password,
    hash_token,
    utcnow,
)
from app.models.command import Command, CommandEvent
from app.models.device import ClaimCode, Device, DeviceCredential
from app.models.enums import ClaimCodeStatus, CommandStatus, DeviceStatus, PlanStatus
from app.models.station import Station
from app.models.user import User
from app.models.optimization import Plan
from app.models.preference import PreferenceVersion
from app.models.station import StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.schemas.device_api import TelemetryItem

MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_TELEMETRY_AGE = timedelta(days=400)
LATE_TELEMETRY_THRESHOLD = timedelta(minutes=5)


class DeviceServiceError(Exception):
    pass


def create_claim_code(db: Session, station: Station, created_by: User) -> tuple[ClaimCode, str]:
    settings = get_settings()
    raw_code, prefix = generate_claim_code()
    claim = ClaimCode(
        station_id=station.id,
        code_hash=hash_token(raw_code),
        code_prefix=prefix,
        status=ClaimCodeStatus.pending.value,
        created_by_user_id=created_by.id,
        expires_at=expires_in(minutes=settings.device_claim_code_ttl_minutes),
    )
    db.add(claim)
    db.flush()
    return claim, raw_code


def revoke_device(db: Session, device: Device, reason: str) -> None:
    device.status = DeviceStatus.revoked.value
    device.revoked_at = utcnow()
    device.revoked_reason = reason
    db.add(device)
    db.execute(
        update(DeviceCredential)
        .where(DeviceCredential.device_id == device.id, DeviceCredential.is_active.is_(True))
        .values(is_active=False, revoked_at=utcnow())
    )
    db.flush()


def claim_device(db: Session, claim_code_raw: str, device_name: str, hardware_info: dict) -> tuple[Device, str]:
    code_hash = hash_token(claim_code_raw.strip().upper())
    claim = db.scalar(select(ClaimCode).where(ClaimCode.code_hash == code_hash))
    if claim is None or claim.status != ClaimCodeStatus.pending.value:
        raise DeviceServiceError("Cod de asociere invalid sau deja folosit.")
    if claim.expires_at < utcnow():
        claim.status = ClaimCodeStatus.expired.value
        db.add(claim)
        db.flush()
        raise DeviceServiceError("Cod de asociere expirat.")

    device = Device(
        station_id=claim.station_id,
        name=device_name,
        status=DeviceStatus.active.value,
        capabilities=hardware_info or {},
    )
    db.add(device)
    db.flush()

    raw_secret = generate_opaque_token(32)
    credential = DeviceCredential(device_id=device.id, secret_hash=hash_password(raw_secret))
    db.add(credential)

    claim.status = ClaimCodeStatus.claimed.value
    claim.claimed_at = utcnow()
    claim.claimed_device_id = device.id
    db.add(claim)
    db.flush()

    return device, raw_secret


def rotate_credential(db: Session, device: Device) -> str:
    db.execute(
        update(DeviceCredential)
        .where(DeviceCredential.device_id == device.id, DeviceCredential.is_active.is_(True))
        .values(is_active=False, revoked_at=utcnow())
    )
    raw_secret = generate_opaque_token(32)
    credential = DeviceCredential(device_id=device.id, secret_hash=hash_password(raw_secret))
    db.add(credential)
    db.flush()
    return raw_secret


def record_heartbeat(db: Session, device: Device, boot_id: str, firmware_version: str | None, capabilities: dict) -> Device:
    device.last_heartbeat_at = utcnow()
    device.last_boot_id = boot_id
    if firmware_version:
        device.firmware_version = firmware_version
    if capabilities:
        device.capabilities = {**(device.capabilities or {}), **capabilities}
    db.add(device)
    db.flush()
    return device


def ingest_telemetry_batch(db: Session, device: Device, items: list[TelemetryItem]) -> tuple[int, int, int, list[str]]:
    now = utcnow()
    rows = []
    errors: list[str] = []
    rejected = 0

    for idx, item in enumerate(items):
        if item.measured_at > now + MAX_FUTURE_SKEW:
            rejected += 1
            errors.append(f"item {idx}: measured_at in viitor (peste toleranta de {MAX_FUTURE_SKEW}).")
            continue
        if item.measured_at < now - MAX_TELEMETRY_AGE:
            rejected += 1
            errors.append(f"item {idx}: measured_at prea vechi (peste {MAX_TELEMETRY_AGE.days} zile).")
            continue

        is_late = (now - item.measured_at) > LATE_TELEMETRY_THRESHOLD
        rows.append(
            dict(
                id=uuid.uuid4(),
                device_id=device.id,
                station_id=device.station_id,
                boot_id=item.boot_id,
                sequence=item.sequence,
                schema_version=item.schema_version,
                measured_at=item.measured_at,
                received_at=now,
                pv_power_w=item.pv_power_w,
                load_power_w=item.load_power_w,
                battery_power_w=item.battery_power_w,
                grid_power_w=item.grid_power_w,
                battery_soc_percent=item.battery_soc_percent,
                ev_connected=item.ev_connected,
                ev_power_w=item.ev_power_w,
                quality_flags=item.quality_flags,
                raw_payload=item.raw_payload,
                is_simulated=bool(item.raw_payload.get("simulated", False)),
                is_late=is_late,
            )
        )

    accepted = 0
    duplicates = 0
    if rows:
        stmt = (
            pg_insert(TelemetryRaw)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["device_id", "boot_id", "sequence"])
            .returning(TelemetryRaw.id)
        )
        result = db.execute(stmt)
        inserted_ids = result.fetchall()
        accepted = len(inserted_ids)
        duplicates = len(rows) - accepted
        db.flush()

    return accepted, duplicates, rejected, errors


def get_active_station_config(db: Session, station_id: uuid.UUID) -> tuple[StationConfigVersion | None, PreferenceVersion | None]:
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.station_id == station_id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )
    return config, preference


def get_active_plan(db: Session, station_id: uuid.UUID) -> Plan | None:
    return db.scalar(
        select(Plan)
        .where(
            Plan.station_id == station_id,
            Plan.status.in_(
                [PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value]
            ),
        )
        .order_by(Plan.version.desc())
        .limit(1)
    )


def accept_plan(db: Session, device: Device, version: int) -> Plan:
    plan = get_active_plan(db, device.station_id)
    if plan is None or plan.version != version:
        raise DeviceServiceError("Nu exista un plan publicat cu aceasta versiune.")
    if plan.status == PlanStatus.published.value:
        plan.status = PlanStatus.accepted_by_device.value
        plan.accepted_at = utcnow()
        plan.accepted_by_device_id = device.id
        db.add(plan)
        db.flush()
    return plan


def list_pending_commands(db: Session, device: Device) -> list[Command]:
    now = utcnow()
    expired = db.scalars(
        select(Command).where(
            Command.device_id == device.id,
            Command.status.in_([CommandStatus.created.value, CommandStatus.delivered.value, CommandStatus.accepted.value]),
            Command.expires_at < now,
        )
    ).all()
    for cmd in expired:
        cmd.status = CommandStatus.expired.value
        db.add(cmd)
        db.add(CommandEvent(command_id=cmd.id, event_type="expired", source="system", message="Comanda a expirat inainte de confirmare."))
    if expired:
        db.flush()

    pending = db.scalars(
        select(Command)
        .where(
            Command.device_id == device.id,
            Command.status.in_([CommandStatus.created.value, CommandStatus.delivered.value]),
            Command.expires_at >= now,
            Command.valid_from <= now,
        )
        .order_by(Command.valid_from)
    ).all()

    for cmd in pending:
        if cmd.status == CommandStatus.created.value:
            cmd.status = CommandStatus.delivered.value
            cmd.delivered_at = now
            db.add(cmd)
            db.add(CommandEvent(command_id=cmd.id, event_type="delivered", source="system"))
    if pending:
        db.flush()

    return pending


def acknowledge_command(db: Session, device: Device, command_id: uuid.UUID, status_value: str, reason: str | None) -> Command:
    command = db.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise DeviceServiceError("Comanda nu exista pentru acest dispozitiv.")
    if command.status not in (CommandStatus.delivered.value, CommandStatus.created.value):
        raise DeviceServiceError(f"Comanda este in starea '{command.status}', nu poate fi confirmata/respinsa acum.")
    if command.expires_at < utcnow():
        command.status = CommandStatus.expired.value
        db.add(command)
        db.add(CommandEvent(command_id=command.id, event_type="expired", source="system"))
        db.flush()
        raise DeviceServiceError("Comanda a expirat.")

    if status_value == "accepted":
        command.status = CommandStatus.accepted.value
        command.accepted_at = utcnow()
    else:
        command.status = CommandStatus.rejected.value
    db.add(command)
    db.add(
        CommandEvent(
            command_id=command.id,
            event_type=status_value,
            source="device",
            message=reason,
        )
    )
    db.flush()
    return command


def report_command_result(
    db: Session, device: Device, command_id: uuid.UUID, status_value: str, details: dict, error_message: str | None
) -> Command:
    command = db.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise DeviceServiceError("Comanda nu exista pentru acest dispozitiv.")
    if command.status != CommandStatus.accepted.value:
        raise DeviceServiceError(
            f"Comanda este in starea '{command.status}'; rezultatul poate fi raportat doar dupa acceptare."
        )

    if status_value == "executed":
        command.status = CommandStatus.executed.value
        command.executed_at = utcnow()
    else:
        command.status = CommandStatus.failed.value
        command.last_error = (error_message or "")[:1000]
    db.add(command)
    db.add(
        CommandEvent(
            command_id=command.id,
            event_type=status_value,
            source="device",
            payload=details,
            message=error_message,
        )
    )
    db.flush()
    return command
