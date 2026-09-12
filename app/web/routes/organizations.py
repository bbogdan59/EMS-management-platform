from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import OrganizationAccess, get_current_user
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.rbac import can_manage_organization
from app.core.security import utcnow
from app.database import get_db
from app.models.organization import Membership
from app.models.user import Invitation, User
from app.schemas.station_forms import StationCreateInput
from app.services import auth_service, membership_service, station_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()


def _organization_context(db: Session, organization, role: str, user: User, *, invitation_token=None):
    return {
        "organization": organization,
        "stations": organization.stations,
        "memberships": membership_service.list_members(db, organization),
        "pending_invitations": membership_service.list_pending_invitations(db, organization),
        "now": utcnow(),
        "current_user_id": user.id,
        "my_role": role,
        "can_manage": can_manage_organization(role),
        "organization_roles": sorted(auth_service.ORGANIZATION_ROLES),
        "invitation_token": invitation_token,
        **build_nav_context(db, user),
    }


def _redirect_with_error(organization_id: uuid.UUID, message: str) -> RedirectResponse:
    query = urlencode({"error": message})
    return RedirectResponse(f"/organizations/{organization_id}?{query}", status_code=303)


@router.get("/organizations/{organization_id}")
def organization_detail(
    request: Request,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    organization, role = org_role
    context = {
        **_organization_context(db, organization, role, user),
        "invite_link": request.query_params.get("invite_link"),
        "errors": request.query_params.getlist("error"),
    }
    return templates.TemplateResponse(request, "organizations/detail.html", context)


def _none_if_blank(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value


@router.post("/organizations/{organization_id}/stations", dependencies=[Depends(verify_csrf)])
def create_station(
    request: Request,
    name: str = Form(...),
    timezone: str = Form("Europe/Bucharest"),
    latitude: str = Form(...),
    longitude: str = Form(...),
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

    raw = {
        "name": name,
        "timezone": timezone,
        "latitude": latitude,
        "longitude": longitude,
        "pv_installed_power_kw": pv_installed_power_kw,
        "inverter_power_kw": inverter_power_kw,
        "battery_reference_capacity_kwh": _none_if_blank(battery_reference_capacity_kwh),
        "battery_max_charge_power_kw": _none_if_blank(battery_max_charge_power_kw),
        "battery_max_discharge_power_kw": _none_if_blank(battery_max_discharge_power_kw),
        "grid_import_limit_kw": _none_if_blank(grid_import_limit_kw),
        "grid_export_limit_kw": _none_if_blank(grid_export_limit_kw),
        "ev_enabled": bool(ev_enabled),
    }
    try:
        validated = StationCreateInput.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc']) or 'formular'}: {e['msg']}" for e in exc.errors()]
        query = urlencode([("error", e) for e in errors])
        return RedirectResponse(f"/organizations/{organization.id}?{query}", status_code=303)

    station = station_service.create_station(
        db,
        organization,
        name=validated.name,
        timezone=validated.timezone,
        pv_installed_power_kw=validated.pv_installed_power_kw,
        inverter_power_kw=validated.inverter_power_kw,
        battery_reference_capacity_kwh=validated.battery_reference_capacity_kwh,
        battery_available_capacity_kwh=validated.battery_reference_capacity_kwh,
        battery_max_charge_power_kw=validated.battery_max_charge_power_kw,
        battery_max_discharge_power_kw=validated.battery_max_discharge_power_kw,
        battery_charge_efficiency=None,
        battery_discharge_efficiency=None,
        grid_import_limit_kw=validated.grid_import_limit_kw,
        grid_export_limit_kw=validated.grid_export_limit_kw,
        ev_enabled=validated.ev_enabled,
        ev_battery_capacity_kwh=None,
        ev_max_charge_power_kw=None,
        latitude=validated.latitude,
        longitude=validated.longitude,
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
    organization, current_role = org_role
    try:
        invitation, raw_token = auth_service.create_invitation(db, organization, email, role, user.id)
    except auth_service.AuthError as exc:
        db.rollback()
        context = _organization_context(db, organization, current_role, user)
        context["invitation_error"] = str(exc)
        return templates.TemplateResponse(request, "organizations/detail.html", context, status_code=400)
    record_audit(
        db, action="invitation_created", resource_type="invitation", resource_id=str(invitation.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=organization.id,
        metadata={"invited_email": email, "role": role},
    )
    db.commit()

    from app.config import get_settings

    settings = get_settings()
    token = raw_token if settings.environment != "production" else None
    response = templates.TemplateResponse(
        request,
        "organizations/detail.html",
        _organization_context(db, organization, current_role, user, invitation_token=token),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _get_membership_in_org(db: Session, organization_id: uuid.UUID, membership_id: uuid.UUID) -> Membership | None:
    membership = db.get(Membership, membership_id)
    if membership is None or membership.organization_id != organization_id:
        return None
    return membership


def _get_invitation_in_org(db: Session, organization_id: uuid.UUID, invitation_id: uuid.UUID) -> Invitation | None:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != organization_id:
        return None
    return invitation


@router.post("/organizations/{organization_id}/members/{membership_id}/role", dependencies=[Depends(verify_csrf)])
def change_member_role(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    role: str = Form(...),
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _current_role = org_role
    membership = _get_membership_in_org(db, organization.id, membership_id)
    if membership is None:
        return _redirect_with_error(organization_id, "Membership inexistent in aceasta organizatie.")
    try:
        membership_service.change_role(db, organization, membership, role, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return _redirect_with_error(organization_id, str(exc))
    db.commit()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/deactivate", dependencies=[Depends(verify_csrf)])
def deactivate_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _current_role = org_role
    membership = _get_membership_in_org(db, organization.id, membership_id)
    if membership is None:
        return _redirect_with_error(organization_id, "Membership inexistent in aceasta organizatie.")
    try:
        membership_service.deactivate_member(db, organization, membership, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return _redirect_with_error(organization_id, str(exc))
    db.commit()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/reactivate", dependencies=[Depends(verify_csrf)])
def reactivate_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _current_role = org_role
    membership = _get_membership_in_org(db, organization.id, membership_id)
    if membership is None:
        return _redirect_with_error(organization_id, "Membership inexistent in aceasta organizatie.")
    try:
        membership_service.reactivate_member(db, organization, membership, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return _redirect_with_error(organization_id, str(exc))
    db.commit()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/remove", dependencies=[Depends(verify_csrf)])
def remove_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _current_role = org_role
    membership = _get_membership_in_org(db, organization.id, membership_id)
    if membership is None:
        return _redirect_with_error(organization_id, "Membership inexistent in aceasta organizatie.")
    try:
        membership_service.remove_member(db, organization, membership, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return _redirect_with_error(organization_id, str(exc))
    db.commit()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/invitations/{invitation_id}/resend", dependencies=[Depends(verify_csrf)])
def resend_invitation(
    request: Request,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, current_role = org_role
    invitation = _get_invitation_in_org(db, organization.id, invitation_id)
    if invitation is None:
        return _redirect_with_error(organization_id, "Invitatie inexistenta in aceasta organizatie.")
    try:
        raw_token = membership_service.resend_invitation(db, organization, invitation, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return _redirect_with_error(organization_id, str(exc))
    db.commit()

    from app.config import get_settings

    settings = get_settings()
    token = raw_token if settings.environment != "production" else None
    response = templates.TemplateResponse(
        request,
        "organizations/detail.html",
        _organization_context(db, organization, current_role, user, invitation_token=token),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/organizations/{organization_id}/invitations/{invitation_id}/cancel", dependencies=[Depends(verify_csrf)])
def cancel_invitation(
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    db: Session = Depends(get_db),
    org_role: tuple = Depends(OrganizationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    organization, _current_role = org_role
    invitation = _get_invitation_in_org(db, organization.id, invitation_id)
    if invitation is None:
        return _redirect_with_error(organization_id, "Invitatie inexistenta in aceasta organizatie.")
    try:
        membership_service.cancel_invitation(db, organization, invitation, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return _redirect_with_error(organization_id, str(exc))
    db.commit()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)
