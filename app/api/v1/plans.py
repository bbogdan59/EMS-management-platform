from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.database import get_db
from app.schemas.device_api import (
    ActivePlanResponse,
    PlanIntervalOut,
    StationConfigOut,
)
from app.services import device_service

router = APIRouter()


@router.get("/config", response_model=StationConfigOut)
def get_config(device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    config, preference = device_service.get_active_station_config(db, device.station_id)
    if config is None or preference is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Statia nu are configuratie/preferinte publicate inca.")

    from app.models.station import Station

    station = db.get(Station, device.station_id)
    return StationConfigOut(
        station_id=device.station_id,
        timezone=station.timezone,
        execution_mode=station.execution_mode,
        inverter_power_kw=config.inverter_power_kw,
        battery_max_charge_power_kw=config.battery_max_charge_power_kw,
        battery_max_discharge_power_kw=config.battery_max_discharge_power_kw,
        grid_import_limit_kw=config.grid_import_limit_kw,
        grid_export_limit_kw=config.grid_export_limit_kw,
        allow_grid_charge=preference.allow_grid_charge,
        allow_battery_export=preference.allow_battery_export,
        min_reserve_soc_percent=preference.min_reserve_soc_percent,
        max_normal_soc_percent=preference.max_normal_soc_percent,
        config_version=config.version,
        preference_version=preference.version,
    )


@router.get("/plan/active", response_model=ActivePlanResponse)
def get_active_plan(device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    plan = device_service.get_active_plan(db, device.station_id)
    if plan is None:
        return ActivePlanResponse(plan_id=None, version=None, status=None, published_at=None, intervals=[])

    from app.core.security import utcnow

    now = utcnow()
    intervals = [
        PlanIntervalOut(
            interval_start=pi.interval_start,
            interval_end=pi.interval_end,
            battery_power_target_kw=pi.battery_power_target_kw,
            grid_power_target_kw=pi.grid_power_target_kw,
            battery_soc_target_percent=pi.battery_soc_target_percent,
            ev_charge_power_kw=pi.ev_charge_power_kw,
            explanation=pi.explanation,
        )
        for pi in plan.intervals
        if pi.interval_end >= now
    ]
    return ActivePlanResponse(
        plan_id=plan.id,
        version=plan.version,
        status=plan.status,
        execution_mode=plan.execution_mode,
        published_at=plan.published_at,
        intervals=intervals,
    )


@router.post("/plan/accept", response_model=ActivePlanResponse)
def accept_plan(version: int, device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    """Marcheaza explicit acceptarea planului curent de catre dispozitiv.
    Separat de simpla citire (GET /plan/active) -- reflecta pasul
    'acceptarea planului de dispozitiv' din ciclul de viata al planului."""
    try:
        device_service.accept_plan(db, device, version)
    except device_service.DeviceServiceError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    db.commit()
    return get_active_plan(device=device, db=db)
