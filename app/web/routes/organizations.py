from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import OrganizationAccess, get_current_user
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.rbac import can_manage_organization
from app.database import get_db
from app.models.organization import Membership
from app.models.user import User
from app.services import auth_service, station_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()


def _dec(value: str | None) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


@router.get("/organizations/{organization_id}")
def organization_detail(
    request: Request,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    organization, role = org_role
    memberships = db.scalars(
        select(Membership).where(Membership.organization_id == organization.id)
    ).all()
    member_rows = []
    for m in memberships:
        u = db.get(User, m.user_id)
        member_rows.append({"email": u.email if u else "?", "role": m.role})

    context = {
        "organization": organization,
        "stations": organization.stations,
        "memberships": member_rows,
        "my_role": role,
        "can_manage": can_manage_organization(role),
        "invite_link": request.query_params.get("invite_link"),
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "organizations/detail.html", context)


@router.post("/organizations/{organization_id}/stations", dependencies=[Depends(verify_csrf)])
def create_station(
    request: Request,
    name: str = Form(...),
    timezone: str = Form("Europe/Bucharest"),
    pv_installed_power_kw: str = Form(...),
    inverter_power_kw: str = Form(...),
    battery_reference_capacity_kwh: str | None = Form(None),
    battery_max_charge_power_kw: str | None = Form(None),
    battery_max_discharge_power_kw: str | None = Form(None),
    grid_import_limit_kw: str | None = Form(None),
    grid_export_limit_kw: str | None = Form(None),
    ev_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _role = org_role
    station = station_service.create_station(
        db,
        organization,
        name=name,
        timezone=timezone,
        pv_installed_power_kw=_dec(pv_installed_power_kw) or Decimal("0"),
        inverter_power_kw=_dec(inverter_power_kw) or Decimal("0"),
        battery_reference_capacity_kwh=_dec(battery_reference_capacity_kwh),
        battery_available_capacity_kwh=_dec(battery_reference_capacity_kwh),
        battery_max_charge_power_kw=_dec(battery_max_charge_power_kw),
        battery_max_discharge_power_kw=_dec(battery_max_discharge_power_kw),
        battery_charge_efficiency=None,
        battery_discharge_efficiency=None,
        grid_import_limit_kw=_dec(grid_import_limit_kw),
        grid_export_limit_kw=_dec(grid_export_limit_kw),
        ev_enabled=bool(ev_enabled),
        ev_battery_capacity_kwh=None,
        ev_max_charge_power_kw=None,
        latitude=None,
        longitude=None,
        created_by=user,
    )
    record_audit(
        db, action="station_created", resource_type="station", resource_id=str(station.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=organization.id, station_id=station.id,
    )
    db.commit()
    return RedirectResponse(f"/organizations/{organization.id}", status_code=303)


@router.post("/organizations/{organization_id}/invitations", dependencies=[Depends(verify_csrf)])
def invite_member(
    request: Request,
    email: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _role = org_role
    invitation, raw_token = auth_service.create_invitation(db, organization, email, role, user.id)
    record_audit(
        db, action="invitation_created", resource_type="invitation", resource_id=str(invitation.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=organization.id,
        metadata={"invited_email": email, "role": role},
    )
    db.commit()

    from app.config import get_settings

    settings = get_settings()
    link = f"/accept-invitation?token={raw_token}" if settings.environment != "production" else None
    suffix = f"&invite_link={link}" if link else ""
    return RedirectResponse(f"/organizations/{organization.id}?x=1{suffix}", status_code=303)
