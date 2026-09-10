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
from app.models.enums import CommandStatus, CommandType, PlanStatus
from app.models.optimization import Plan, PlanInterval
from app.models.station import Station


def dispatch_due_commands(db: Session) -> list[Command]:
    now = utcnow()
    live_stations = db.scalars(select(Station).where(Station.execution_mode == "live")).all()
    created: list[Command] = []

    for station in live_stations:
        plan = db.scalar(
            select(Plan).where(
                Plan.station_id == station.id,
                Plan.status.in_([PlanStatus.accepted_by_device.value, PlanStatus.executing.value]),
            ).order_by(Plan.version.desc()).limit(1)
        )
        if plan is None:
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

        device = _find_active_device(db, station.id)
        if device is None:
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


def _find_active_device(db: Session, station_id):
    from app.models.device import Device
    from app.models.enums import DeviceStatus

    return db.scalar(
        select(Device).where(Device.station_id == station_id, Device.status == DeviceStatus.active.value).limit(1)
    )
