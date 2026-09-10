from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.rbac import can_manage_station_config, can_modify_operational_settings
from app.database import get_db
from app.models.device import ClaimCode, Device
from app.models.station import PanelGroup, StationConfigVersion
from app.models.tariff import Tariff
from app.models.user import User
from app.services import device_service, station_service, tariff_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()


def _dec(value: str | None, default: Decimal | None = None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


# --- Configuratie tehnica ---


@router.get("/stations/{station_id}/config")
def station_config_form(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    station, role = station_role
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    history = db.scalars(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
    ).all()
    context = {
        "station": station,
        "config": config,
        "history": history,
        "can_edit": can_manage_station_config(role),
        **build_nav_context(db, user, station.id),
    }
    return templates.TemplateResponse(request, "stations/config.html", context)


@router.post("/stations/{station_id}/config", dependencies=[Depends(verify_csrf)])
def station_config_submit(
    request: Request,
    pv_installed_power_kw: str = Form(...),
    inverter_power_kw: str = Form(...),
    battery_reference_capacity_kwh: str | None = Form(None),
    battery_available_capacity_kwh: str | None = Form(None),
    battery_max_charge_power_kw: str | None = Form(None),
    battery_max_discharge_power_kw: str | None = Form(None),
    battery_charge_efficiency: str | None = Form(None),
    battery_discharge_efficiency: str | None = Form(None),
    grid_import_limit_kw: str | None = Form(None),
    grid_export_limit_kw: str | None = Form(None),
    ev_enabled: str | None = Form(None),
    ev_battery_capacity_kwh: str | None = Form(None),
    ev_max_charge_power_kw: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    version = station_service.next_config_version(db, station)
    config = StationConfigVersion(
        station_id=station.id,
        version=version,
        created_by_user_id=user.id,
        pv_installed_power_kw=_dec(pv_installed_power_kw, Decimal("0")),
        inverter_power_kw=_dec(inverter_power_kw, Decimal("0")),
        battery_reference_capacity_kwh=_dec(battery_reference_capacity_kwh),
        battery_available_capacity_kwh=_dec(battery_available_capacity_kwh) or _dec(battery_reference_capacity_kwh),
        battery_max_charge_power_kw=_dec(battery_max_charge_power_kw),
        battery_max_discharge_power_kw=_dec(battery_max_discharge_power_kw),
        battery_charge_efficiency=_dec(battery_charge_efficiency, Decimal("0.95")),
        battery_discharge_efficiency=_dec(battery_discharge_efficiency, Decimal("0.95")),
        grid_import_limit_kw=_dec(grid_import_limit_kw),
        grid_export_limit_kw=_dec(grid_export_limit_kw),
        ev_enabled=bool(ev_enabled),
        ev_battery_capacity_kwh=_dec(ev_battery_capacity_kwh),
        ev_max_charge_power_kw=_dec(ev_max_charge_power_kw),
        notes=notes,
    )
    db.add(config)
    db.flush()
    db.add(
        PanelGroup(
            station_id=station.id,
            config_version_id=config.id,
            name="Grup principal",
            power_kwp=config.pv_installed_power_kw,
            azimuth_degrees=Decimal("180"),
            tilt_degrees=Decimal("30"),
        )
    )
    record_audit(
        db, action="station_config_updated", resource_type="station_config", resource_id=str(config.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        metadata={"version": version},
    )
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/config", status_code=303)


# --- Preferinte ---


@router.get("/stations/{station_id}/preferences")
def preferences_form(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    from app.models.preference import PreferenceVersion

    station, role = station_role
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.station_id == station.id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )
    context = {
        "station": station,
        "preference": preference,
        "can_edit": can_modify_operational_settings(role),
        **build_nav_context(db, user, station.id),
    }
    return templates.TemplateResponse(request, "stations/preferences.html", context)


@router.post("/stations/{station_id}/preferences", dependencies=[Depends(verify_csrf)])
def preferences_submit(
    request: Request,
    min_reserve_soc_percent: str = Form(...),
    max_normal_soc_percent: str = Form(...),
    max_optimization_energy_kwh: str | None = Form(None),
    allow_grid_charge: str | None = Form(None),
    allow_battery_export: str | None = Form(None),
    max_efc_per_day: str | None = Form(None),
    max_efc_per_month: str | None = Form(None),
    priority: str = Form("cost"),
    ev_required_energy_kwh: str | None = Form(None),
    ev_departure_time: str | None = Form(None),
    arbitrage_min_benefit_lei: str = Form("0"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="operator")),
    user: User = Depends(get_current_user),
):
    from app.models.preference import PreferenceVersion

    station, _role = station_role
    version = station_service.next_preference_version(db, station)

    ev_time = None
    if ev_departure_time:
        try:
            h, m = ev_departure_time.split(":")
            from datetime import time as dtime

            ev_time = dtime(int(h), int(m))
        except ValueError:
            ev_time = None

    preference = PreferenceVersion(
        station_id=station.id,
        version=version,
        created_by_user_id=user.id,
        min_reserve_soc_percent=_dec(min_reserve_soc_percent, Decimal("15")),
        max_normal_soc_percent=_dec(max_normal_soc_percent, Decimal("95")),
        max_optimization_energy_kwh=_dec(max_optimization_energy_kwh),
        allow_grid_charge=bool(allow_grid_charge),
        allow_battery_export=bool(allow_battery_export),
        max_efc_per_day=_dec(max_efc_per_day),
        max_efc_per_month=_dec(max_efc_per_month),
        priority=priority,
        ev_required_energy_kwh=_dec(ev_required_energy_kwh),
        ev_departure_time=ev_time,
        arbitrage_min_benefit_lei=_dec(arbitrage_min_benefit_lei, Decimal("0")),
    )

    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    preference.conflict_warnings = station_service.detect_preference_conflicts(preference, config)

    db.add(preference)
    record_audit(
        db, action="preferences_updated", resource_type="preference", resource_id=str(station.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        metadata={"version": version, "conflicts": preference.conflict_warnings},
    )
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/preferences", status_code=303)


# --- Tarife ---


@router.get("/stations/{station_id}/tariffs")
def tariffs_page(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    station, role = station_role
    tariffs = db.scalars(select(Tariff).where(Tariff.station_id == station.id)).all()
    context = {
        "station": station,
        "tariffs": tariffs,
        "can_edit": can_manage_station_config(role),
        **build_nav_context(db, user, station.id),
    }
    return templates.TemplateResponse(request, "stations/tariffs.html", context)


@router.post("/stations/{station_id}/tariffs", dependencies=[Depends(verify_csrf)])
def tariffs_submit(
    request: Request,
    direction: str = Form(...),
    kind: str = Form(...),
    name: str = Form(...),
    fixed_price_lei_per_kwh: str | None = Form(None),
    opcom_margin_lei_per_kwh: str | None = Form(None),
    fixed_monthly_fee_lei: str = Form("0"),
    variable_component_lei_per_kwh: str = Form("0"),
    settlement_method: str = Form("net_metering_15min"),
    settlement_interval_days: int = Form(30),
    economic_calculation_disabled: str | None = Form(None),
    limitation_note: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    tariff = tariff_service.get_or_create_tariff(db, station, direction, kind, name)
    tariff_service.add_tariff_version(
        db,
        tariff,
        valid_from=datetime.now(timezone.utc),
        fixed_price_lei_per_kwh=_dec(fixed_price_lei_per_kwh),
        opcom_margin_lei_per_kwh=_dec(opcom_margin_lei_per_kwh),
        fixed_monthly_fee_lei=_dec(fixed_monthly_fee_lei, Decimal("0")),
        variable_component_lei_per_kwh=_dec(variable_component_lei_per_kwh, Decimal("0")),
        settlement_method=settlement_method,
        settlement_interval_days=settlement_interval_days,
        economic_calculation_disabled=bool(economic_calculation_disabled),
        limitation_note=limitation_note,
    )
    record_audit(
        db, action="tariff_version_created", resource_type="tariff", resource_id=str(tariff.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
    )
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/tariffs", status_code=303)


# --- Dispozitive / coduri de asociere ---


@router.get("/stations/{station_id}/devices")
def devices_page(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="operator")),
    user: User = Depends(get_current_user),
):
    station, role = station_role
    devices = db.scalars(select(Device).where(Device.station_id == station.id)).all()
    claim_codes = db.scalars(
        select(ClaimCode).where(ClaimCode.station_id == station.id).order_by(ClaimCode.created_at.desc()).limit(10)
    ).all()
    context = {
        "station": station,
        "devices": devices,
        "claim_codes": claim_codes,
        "can_edit": can_manage_station_config(role),
        "new_claim_code": request.query_params.get("new_code"),
        **build_nav_context(db, user, station.id),
    }
    return templates.TemplateResponse(request, "stations/devices.html", context)


@router.post("/stations/{station_id}/claim-codes", dependencies=[Depends(verify_csrf)])
def create_claim_code(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    claim, raw_code = device_service.create_claim_code(db, station, user)
    record_audit(
        db, action="claim_code_created", resource_type="claim_code", resource_id=str(claim.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
    )
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/devices?new_code={raw_code}", status_code=303)


@router.post("/stations/{station_id}/devices/{device_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke_device(
    request: Request,
    station_id: uuid.UUID,
    device_id: uuid.UUID,
    reason: str = Form("Revocat manual din UI"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    device = db.get(Device, device_id)
    if device is None or device.station_id != station.id:
        return RedirectResponse(f"/stations/{station.id}/devices", status_code=303)
    device_service.revoke_device(db, device, reason)
    record_audit(
        db, action="device_revoked", resource_type="device", resource_id=str(device.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id, metadata={"reason": reason},
    )
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/devices", status_code=303)
