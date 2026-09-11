"""Traduce intervalul curent al unui plan ACCEPTAT de dispozitiv intr-o
comanda semantica, concreta si cu expirare -- doar pentru statii in
execution_mode='live'. In modul shadow (implicit) nu se emit comenzi:
planul e doar informativ, per cerinta ca platforma sa nu autorizeze
executia fizica implicit."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.command import Command
from app.models.device import Device
from app.models.enums import (
    CommandStatus,
    CommandType,
    DeviceStatus,
    OptimizationRunStatus,
    PlanStatus,
)
from app.models.optimization import Plan, PlanInterval
from app.models.preference import PreferenceVersion
from app.models.station import Station, StationConfigVersion


def plan_allows_dispatch(db: Session, plan: Plan, station: Station, now) -> bool:
    """Recheck persisted authorization at dispatch/delivery time, not only at solve time."""
    if not station.is_active or station.execution_mode != "live" or plan.execution_mode != "live":
        return False
    if plan.status not in (PlanStatus.accepted_by_device.value, PlanStatus.executing.value):
        return False
    run = plan.optimization_run
    if run is None or run.is_fallback or run.status != OptimizationRunStatus.succeeded.value:
        return False
    preference = db.scalar(
        select(PreferenceVersion).where(PreferenceVersion.station_id == station.id)
        .order_by(PreferenceVersion.version.desc()).limit(1)
    )
    config = db.scalar(
        select(StationConfigVersion).where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc()).limit(1)
    )
    if preference is None or config is None:
        return False
    if run.preference_version_id != preference.id or run.station_config_version_id != config.id:
        return False
    return not (preference.automation_suspended_until and preference.automation_suspended_until > now)


def command_allows_delivery(db: Session, command: Command, device: Device, now) -> bool:
    from app.services.inverter_config_service import COMMAND_TYPE, delivery_allowed

    if command.type == COMMAND_TYPE:
        return delivery_allowed(db, command, device, now)
    if command.plan_interval_id is None:
        return command.author != "optimizer"
    interval = db.get(PlanInterval, command.plan_interval_id)
    if interval is None:
        return False
    plan = db.get(Plan, interval.plan_id)
    station = db.get(Station, command.station_id)
    return bool(
        plan and station and plan.station_id == station.id == device.station_id
        and plan.accepted_by_device_id == device.id
        and device.status == DeviceStatus.active.value
        and plan_allows_dispatch(db, plan, station, now)
    )


def dispatch_due_commands(db: Session) -> list[Command]:
    now = utcnow()
    live_stations = db.scalars(select(Station).where(Station.execution_mode == "live", Station.is_active.is_(True))).all()
    created: list[Command] = []

    for station in live_stations:
        plan = db.scalar(
            select(Plan).where(
                Plan.station_id == station.id,
                Plan.status.in_([PlanStatus.accepted_by_device.value, PlanStatus.executing.value]),
            ).order_by(Plan.version.desc()).limit(1)
        )
        if plan is None or not plan_allows_dispatch(db, plan, station, now):
            continue

        current_interval = db.scalar(
            select(PlanInterval).where(
                PlanInterval.plan_id == plan.id,
                PlanInterval.interval_start <= now,
                PlanInterval.interval_end > now,
            )
        )
        if current_interval is None:
            continue

        device = db.get(Device, plan.accepted_by_device_id) if plan.accepted_by_device_id else None
        if device is None or device.station_id != station.id or device.status != DeviceStatus.active.value:
            continue

        idempotency_key = f"plan:{plan.id}:interval:{current_interval.interval_start.isoformat()}"
        existing = db.scalar(
            select(Command).where(Command.device_id == device.id, Command.idempotency_key == idempotency_key)
        )
        if existing is not None:
            continue

        command = Command(
            station_id=station.id,
            device_id=device.id,
            plan_interval_id=current_interval.id,
            type=CommandType.set_battery_target_soc.value,
            parameters={
                "target_soc_percent": float(current_interval.battery_soc_target_percent),
                "battery_power_kw": float(current_interval.battery_power_target_kw),
            },
            version=1,
            idempotency_key=idempotency_key,
            status=CommandStatus.created.value,
            author="optimizer",
            reason=current_interval.explanation or "Executie plan de optimizare.",
            valid_from=current_interval.interval_start,
            expires_at=current_interval.interval_end,
        )
        db.add(command)
        created.append(command)
        if plan.status == PlanStatus.accepted_by_device.value:
            plan.status = PlanStatus.executing.value
            db.add(plan)

    db.flush()
    return created
