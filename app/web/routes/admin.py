from __future__ import annotations

import uuid
from datetime import timedelta

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_platform_admin
from app.config import get_settings
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.security import utcnow
from app.database import get_db
from app.models.admin_job import AdminJob
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.command import Command
from app.models.device import Device
from app.models.enums import AdminJobStatus, AdminJobType, AlertStatus, DeviceStatus, PlanStatus
from app.models.firmware import FirmwareDeployment
from app.models.market import ImportRun
from app.models.optimization import OptimizationRun, Plan
from app.models.organization import Membership, Organization
from app.models.preference import PreferenceVersion
from app.models.station import Station, StationConfigVersion
from app.models.task_execution import TaskExecution
from app.models.user import Invitation, User
from app.services import (
    auth_service,
    dashboard_service,
    device_service,
    market_retention_service,
    membership_service,
    optimization_view,
    organization_service,
    station_service,
    task_execution_service,
)
from app.web.context import build_nav_context
from app.web.invitation_ui import invitation_reveal, should_reveal_invitation_link
from app.web.response_headers import apply_no_store_headers
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_platform_admin)])
logger = structlog.get_logger(__name__)

_SAFE_ENQUEUE_ERROR = "Jobul nu a putut fi trimis catre worker. Incercati din nou."


@router.get("")
def overview(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    now = utcnow()
    stations = db.scalars(select(Station)).all()
    devices = db.scalars(select(Device)).all()
    online_devices = sum(1 for d in devices if d.last_heartbeat_at and (now - d.last_heartbeat_at) < timedelta(minutes=5))

    counts = {
        "organizations": db.scalar(select(func.count(Organization.id))),
        "users": db.scalar(select(func.count(User.id))),
        "stations": len(stations),
        "devices": len(devices),
        "devices_online": online_devices,
        "devices_offline": len(devices) - online_devices,
    }

    active_alerts = db.scalars(
        select(Alert).where(Alert.status == AlertStatus.open.value).order_by(Alert.created_at.desc()).limit(20)
    ).all()

    last_opcom = db.scalar(select(ImportRun).order_by(ImportRun.created_at.desc()).limit(1))
    last_optimization = db.scalar(select(OptimizationRun).order_by(OptimizationRun.created_at.desc()).limit(1))

    context = {
        "counts": counts,
        "active_alerts": active_alerts,
        "last_opcom": last_opcom,
        "last_optimization": last_optimization,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/overview.html", context)


@router.get("/organizations")
def organizations_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    orgs = db.scalars(select(Organization).order_by(Organization.name)).all()
    context = {"organizations": orgs, **build_nav_context(db, user)}
    return templates.TemplateResponse(request, "admin/organizations.html", context)


@router.post("/organizations", dependencies=[Depends(verify_csrf)])
def create_organization(
    request: Request,
    name: str = Form(...),
    is_demo: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org = station_service.create_organization(db, name, user, is_demo=bool(is_demo))
    record_audit(
        db, action="organization_created", resource_type="organization", resource_id=str(org.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=org.id,
    )
    db.commit()
    return RedirectResponse("/admin/organizations", status_code=303)


def _admin_organization_context(
    db: Session, request: Request, organization: Organization, user: User, *, invitation_reveal=None
) -> dict:
    """Contextul paginii de backoffice a unei organizatii (issue #24) --
    partajat intre GET (listare) si POST-urile de invitatie (issue #149) care
    trebuie sa reafiseze pagina direct, nu sa redirecteze, ca sa poata arata
    linkul afisat o singura data."""
    member_rows = membership_service.list_members(db, organization)
    pending_invitations = membership_service.list_pending_invitations(db, organization)

    station_rows = []
    for station in db.scalars(select(Station).where(Station.organization_id == organization.id).order_by(Station.name)).all():
        device_count = db.scalar(select(func.count(Device.id)).where(Device.station_id == station.id))
        open_alerts = db.scalar(
            select(func.count(Alert.id)).where(Alert.station_id == station.id, Alert.status == AlertStatus.open.value)
        )
        latest_telemetry = dashboard_service.get_latest_telemetry(db, station.id)
        station_rows.append(
            {
                "station": station,
                "device_count": device_count,
                "open_alerts": open_alerts,
                "last_telemetry_at": latest_telemetry.measured_at if latest_telemetry else None,
            }
        )

    recent_audit = db.scalars(
        select(AuditLog).where(AuditLog.organization_id == organization.id).order_by(AuditLog.occurred_at.desc()).limit(20)
    ).all()

    settings = get_settings()
    return {
        "organization": organization,
        "members": member_rows,
        "pending_invitations": pending_invitations,
        "organization_roles": sorted(auth_service.ORGANIZATION_ROLES),
        "station_rows": station_rows,
        "recent_audit": recent_audit,
        "errors": request.query_params.getlist("error"),
        "now": utcnow(),
        "invitation_reveal": invitation_reveal,
        "invitation_delivery_mode": settings.invitation_delivery_mode,
        **build_nav_context(db, user),
    }


@router.get("/organizations/{organization_id}")
def organization_detail(
    request: Request,
    organization_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pagina de backoffice a organizatiei (issue #24) -- distincta de
    `/organizations/{id}` (autoservire, pentru managerul clientului): aici
    apar campuri cross-tenant (ultima telemetrie, alerte active) si
    controalele de lifecycle (suspendare/arhivare), disponibile DOAR
    platform_admin (acest router e protejat in intregime de acest rol)."""
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)

    context = _admin_organization_context(db, request, organization, user)
    return templates.TemplateResponse(request, "admin/organization_detail.html", context)


@router.post("/organizations/{organization_id}/invitations", dependencies=[Depends(verify_csrf)])
def admin_invite_member(
    request: Request,
    organization_id: uuid.UUID,
    email: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Permite unui platform_admin sa invite direct un membru in orice
    organizatie din backoffice (issue #149), fara sa intre prin impersonare
    in tenant -- actioneaza explicit ca platform_admin (auditat cu
    `actor_user_id`/`actor_label` reale), acelasi serviciu
    (`auth_service.create_invitation`) ca autoservirea organizatiei."""
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    settings = get_settings()
    try:
        invitation, raw_token = auth_service.create_invitation(db, organization, email, role, user.id)
    except auth_service.AuthError as exc:
        db.rollback()
        context = _admin_organization_context(db, request, organization, user)
        context["invitation_error"] = str(exc)
        return templates.TemplateResponse(request, "admin/organization_detail.html", context, status_code=400)
    record_audit(
        db, action="invitation_created", resource_type="invitation", resource_id=str(invitation.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=organization.id,
        metadata={"invited_email": email, "role": role, "delivery_mode": settings.invitation_delivery_mode, "via": "platform_admin"},
    )
    db.commit()

    reveal = invitation_reveal(invitation, raw_token) if should_reveal_invitation_link(settings) else None
    context = _admin_organization_context(db, request, organization, user, invitation_reveal=reveal)
    response = templates.TemplateResponse(request, "admin/organization_detail.html", context)
    return apply_no_store_headers(response)


@router.post("/organizations/{organization_id}/invitations/{invitation_id}/resend", dependencies=[Depends(verify_csrf)])
def admin_resend_invitation(
    request: Request,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    invitation = db.get(Invitation, invitation_id) if organization is not None else None
    if organization is None or invitation is None or invitation.organization_id != organization_id:
        return RedirectResponse("/admin/organizations", status_code=303)
    settings = get_settings()
    try:
        raw_token = membership_service.resend_invitation(db, organization, invitation, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()

    reveal = invitation_reveal(invitation, raw_token) if should_reveal_invitation_link(settings) else None
    context = _admin_organization_context(db, request, organization, user, invitation_reveal=reveal)
    response = templates.TemplateResponse(request, "admin/organization_detail.html", context)
    return apply_no_store_headers(response)


@router.post("/organizations/{organization_id}/edit", dependencies=[Depends(verify_csrf)])
def edit_organization(
    organization_id: uuid.UUID,
    name: str = Form(...),
    billing_email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    organization_service.update_organization_profile(
        db, organization, user, name=name, billing_email=billing_email, notes=notes
    )
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/suspend", dependencies=[Depends(verify_csrf)])
def suspend_organization(
    organization_id: uuid.UUID,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        organization_service.suspend_organization(db, organization, user, reason)
    except organization_service.OrganizationStateError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/reactivate", dependencies=[Depends(verify_csrf)])
def reactivate_organization(
    organization_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        organization_service.reactivate_organization(db, organization, user)
    except organization_service.OrganizationStateError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_organization(
    organization_id: uuid.UUID,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        organization_service.archive_organization(db, organization, user, reason)
    except organization_service.OrganizationStateError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/restore", dependencies=[Depends(verify_csrf)])
def restore_organization(
    organization_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    if organization is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        organization_service.restore_organization(db, organization, user)
    except organization_service.OrganizationStateError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/role", dependencies=[Depends(verify_csrf)])
def admin_change_member_role(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Platform_admin poate atribui/revoca manageri direct din backoffice
    (issue #23) -- fara impersonare: actioneaza explicit ca platform_admin,
    auditat, nu "ca si cum ar fi" un membru al organizatiei."""
    organization = db.get(Organization, organization_id)
    membership = db.get(Membership, membership_id) if organization is not None else None
    if organization is None or membership is None or membership.organization_id != organization_id:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        membership_service.change_role(db, organization, membership, role, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/deactivate", dependencies=[Depends(verify_csrf)])
def admin_deactivate_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    membership = db.get(Membership, membership_id) if organization is not None else None
    if organization is None or membership is None or membership.organization_id != organization_id:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        membership_service.deactivate_member(db, organization, membership, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/reactivate", dependencies=[Depends(verify_csrf)])
def admin_reactivate_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    membership = db.get(Membership, membership_id) if organization is not None else None
    if organization is None or membership is None or membership.organization_id != organization_id:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        membership_service.reactivate_member(db, organization, membership, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/members/{membership_id}/remove", dependencies=[Depends(verify_csrf)])
def admin_remove_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    membership = db.get(Membership, membership_id) if organization is not None else None
    if organization is None or membership is None or membership.organization_id != organization_id:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        membership_service.remove_member(db, organization, membership, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/organizations/{organization_id}/invitations/{invitation_id}/cancel", dependencies=[Depends(verify_csrf)])
def admin_cancel_invitation(
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization = db.get(Organization, organization_id)
    invitation = db.get(Invitation, invitation_id) if organization is not None else None
    if organization is None or invitation is None or invitation.organization_id != organization_id:
        return RedirectResponse("/admin/organizations", status_code=303)
    try:
        membership_service.cancel_invitation(db, organization, invitation, user)
    except membership_service.MembershipError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/organizations/{organization_id}?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse(f"/admin/organizations/{organization_id}", status_code=303)


@router.post("/stations/{station_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_station(
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Arhivare NEDISTRUCTIVA (issue #24): doar `is_active=False`, care deja
    blocheaza dispecerizarea de comenzi live (`command_dispatch_service`) --
    telemetria, planurile si device-urile raman intacte, nicio stergere in
    cascada. Reversibila prin `/restore`."""
    station = db.get(Station, station_id)
    if station is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    station.is_active = False
    db.add(station)
    record_audit(
        db, action="station_archived", resource_type="station", resource_id=str(station.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=station.organization_id, station_id=station.id,
    )
    db.commit()
    return RedirectResponse(f"/admin/organizations/{station.organization_id}", status_code=303)


@router.post("/stations/{station_id}/restore", dependencies=[Depends(verify_csrf)])
def restore_station(
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    station = db.get(Station, station_id)
    if station is None:
        return RedirectResponse("/admin/organizations", status_code=303)
    station.is_active = True
    db.add(station)
    record_audit(
        db, action="station_restored", resource_type="station", resource_id=str(station.id),
        actor_user_id=user.id, actor_label=user.email, organization_id=station.organization_id, station_id=station.id,
    )
    db.commit()
    return RedirectResponse(f"/admin/organizations/{station.organization_id}", status_code=303)


@router.get("/users")
def users_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    users = db.scalars(select(User).order_by(User.email)).all()
    org_map = {o.id: o.name for o in db.scalars(select(Organization)).all()}
    rows = []
    for u in users:
        memberships = db.scalars(select(Membership).where(Membership.user_id == u.id)).all()
        rows.append(
            {
                "user": u,
                "memberships": [{"org": org_map.get(m.organization_id, "?"), "role": m.role} for m in memberships],
            }
        )
    context = {"rows": rows, "errors": request.query_params.getlist("error"), **build_nav_context(db, user)}
    return templates.TemplateResponse(request, "admin/users.html", context)


@router.post("/users/platform-admins", dependencies=[Depends(verify_csrf)])
def create_platform_admin(
    email: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if password != password_confirm:
        return RedirectResponse("/admin/users?error=Parolele+nu+coincid.", status_code=303)
    if len(password) < 10:
        return RedirectResponse("/admin/users?error=Parola+trebuie+sa+aiba+cel+putin+10+caractere.", status_code=303)
    try:
        auth_service.create_platform_admin(db, user, email, full_name, password)
    except auth_service.AuthError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/users?error={exc}", status_code=303)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/operations")
def operations(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    import_runs = db.scalars(select(ImportRun).order_by(ImportRun.created_at.desc()).limit(20)).all()
    optimization_runs = db.scalars(select(OptimizationRun).order_by(OptimizationRun.created_at.desc()).limit(20)).all()
    devices = db.scalars(select(Device).order_by(Device.created_at.desc()).limit(50)).all()
    commands = db.scalars(select(Command).order_by(Command.created_at.desc()).limit(30)).all()
    stations = db.scalars(select(Station).order_by(Station.name)).all()
    admin_jobs = db.scalars(select(AdminJob).order_by(AdminJob.created_at.desc()).limit(20)).all()

    context = {
        "import_runs": import_runs,
        "optimization_runs": optimization_runs,
        "devices": devices,
        "commands": commands,
        "stations": stations,
        "admin_jobs": admin_jobs,
        "market_retention_default_max": market_retention_service.DEFAULT_MAX_ACTIVE_REVISIONS,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/operations.html", context)


@router.post("/operations/import-opcom-csv", dependencies=[Depends(verify_csrf)])
async def import_opcom_csv_upload(
    request: Request,
    delivery_date: str = Form(...),
    csv_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from datetime import date as date_cls

    from app.services import opcom_service

    try:
        parsed_date = date_cls.fromisoformat(delivery_date)
    except ValueError:
        return RedirectResponse("/admin/operations?error=invalid_delivery_date", status_code=303)
    filename = csv_file.filename or "upload.csv"
    if not filename.lower().endswith(".csv"):
        return RedirectResponse("/admin/operations?error=invalid_csv_file", status_code=303)
    raw = await csv_file.read(2_000_001)
    if not raw or len(raw) > 2_000_000:
        return RedirectResponse("/admin/operations?error=invalid_csv_size", status_code=303)
    run = opcom_service.import_opcom_csv(
        db, parsed_date, raw, filename=filename, triggered_by_user_id=user.id
    )
    record_audit(
        db,
        action="opcom_csv_uploaded",
        resource_type="import_run",
        resource_id=str(run.id),
        actor_user_id=user.id,
        actor_label=user.email,
        metadata={"delivery_date": delivery_date, "filename": filename, "status": run.status},
        outcome="success" if run.status in ("succeeded", "unchanged") else "failure",
    )
    db.commit()
    suffix = "csv_imported" if run.status in ("succeeded", "unchanged") else "csv_import_failed"
    return RedirectResponse(f"/admin/operations?status={suffix}", status_code=303)


@router.get("/task-audit")
def task_audit(
    request: Request,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(TaskExecution).order_by(TaskExecution.created_at.desc()).limit(300)
    if status in {"queued", "running", "succeeded", "failed"}:
        stmt = stmt.where(TaskExecution.status == status)
    context = {
        "executions": db.scalars(stmt).all(),
        "available_tasks": task_execution_service.available_manual_tasks(),
        "filter_status": status or "",
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/task_audit.html", context)


@router.post("/task-audit/run", dependencies=[Depends(verify_csrf)])
def run_scheduled_task(
    task_name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        execution, _ = task_execution_service.queue_manual_task(db, task_name, user.id)
    except ValueError:
        return RedirectResponse("/admin/task-audit?error=task_not_allowed", status_code=303)
    except Exception as exc:
        logger.error("manual_task.enqueue_failed", task_name=task_name, exception_type=type(exc).__name__)
        return RedirectResponse("/admin/task-audit?error=enqueue_failed", status_code=303)
    record_audit(
        db,
        action="scheduled_task_triggered_manually",
        resource_type="task_execution",
        resource_id=str(execution.id),
        actor_user_id=user.id,
        actor_label=user.email,
        metadata={"task_name": task_name},
    )
    db.commit()
    return RedirectResponse("/admin/task-audit", status_code=303)


_ACTIVE_ADMIN_JOB_STATUSES = (AdminJobStatus.queued.value, AdminJobStatus.running.value)
# Statusurile de Plan care inseamna "inca in vigoare" -- un plan `completed` deja
# si-a incheiat executia si nu mai e "activ" in sensul de "ar fi inlocuit de o
# reoptimizare", iar unul `superseded` a fost deja inlocuit anterior. Trebuie sa
# ramana identic cu filtrul din `optimization_service._publish_plan`.
_ACTIVE_PLAN_STATUSES = (PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value)


def _active_plan_for_station(db: Session, station_id: uuid.UUID) -> Plan | None:
    return db.scalar(
        select(Plan)
        .where(Plan.station_id == station_id, Plan.status.in_(_ACTIVE_PLAN_STATUSES))
        .order_by(Plan.version.desc())
        .limit(1)
    )


def _lock_admin_job_target(db: Session, dedupe_key: str) -> None:
    """Serializeaza verificarea si crearea unui job pentru aceeasi tinta.

    Verificarea simpla urmata de INSERT permitea doua lansari concurente.
    Lock-ul PostgreSQL este tinut pana la commit-ul randului ``AdminJob``.
    """
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"admin-job:{dedupe_key}", 0))))


def _enqueue_admin_job(db: Session, job: AdminJob, task, user: User) -> bool:
    try:
        async_result = task.delay(str(job.id))
    except Exception as exc:
        logger.error(
            "admin_job.enqueue_failed",
            job_id=str(job.id),
            job_type=job.job_type,
            exception_type=type(exc).__name__,
        )
        job.status = AdminJobStatus.failed.value
        job.finished_at = utcnow()
        job.error_message = _SAFE_ENQUEUE_ERROR
        record_audit(
            db,
            action="admin_job_enqueue_failed",
            resource_type="admin_job",
            resource_id=str(job.id),
            actor_user_id=user.id,
            actor_label=user.email,
            station_id=job.station_id,
            metadata={"job_type": job.job_type},
            outcome="failure",
        )
        db.commit()
        return False

    job.celery_task_id = async_result.id
    db.commit()
    return True


@router.post("/operations/import-opcom", dependencies=[Depends(verify_csrf)])
def trigger_opcom_import(
    request: Request,
    delivery_date: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from datetime import date as date_cls

    from app.workers.tasks import admin_opcom_import_job_task

    try:
        d = date_cls.fromisoformat(delivery_date).isoformat()
    except ValueError:
        return RedirectResponse("/admin/operations?error=invalid_delivery_date", status_code=303)

    _lock_admin_job_target(db, f"opcom:{d}")

    existing = db.scalar(
        select(AdminJob).where(
            AdminJob.job_type == AdminJobType.opcom_import.value,
            AdminJob.status.in_(_ACTIVE_ADMIN_JOB_STATUSES),
            AdminJob.params["delivery_date"].as_string() == d,
        )
    )
    if existing is not None:
        return RedirectResponse("/admin/operations?error=opcom_import_in_progress", status_code=303)

    job = AdminJob(
        job_type=AdminJobType.opcom_import.value,
        status=AdminJobStatus.queued.value,
        params={"delivery_date": d},
        target_label=f"Import OPCOM {d}",
        triggered_by_user_id=user.id,
    )
    db.add(job)
    db.flush()
    record_audit(
        db, action="opcom_import_triggered", resource_type="admin_job", resource_id=str(job.id),
        actor_user_id=user.id, actor_label=user.email, metadata={"delivery_date": d},
    )
    db.commit()

    if not _enqueue_admin_job(db, job, admin_opcom_import_job_task, user):
        return RedirectResponse("/admin/operations?error=enqueue_failed", status_code=303)

    return RedirectResponse("/admin/operations", status_code=303)


@router.post("/operations/market-retention", dependencies=[Depends(verify_csrf)])
def trigger_market_retention(
    request: Request,
    dry_run: str | None = Form(None),
    max_active_revisions: int = Form(market_retention_service.DEFAULT_MAX_ACTIVE_REVISIONS),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Declanseaza manual, pe fundal, arhivarea reviziilor OPCOM excedentare
    (issue #51) -- STRICT NEDISTRUCTIVA, vezi `market_retention_service`.
    `dry_run` (implicit bifat in template) arata ce s-ar arhiva fara sa
    scrie nimic -- utilizatorul trebuie sa debifeze explicit pentru o
    rulare reala."""
    from app.workers.tasks import admin_market_retention_job_task

    if max_active_revisions < 1:
        return RedirectResponse("/admin/operations?error=invalid_max_active_revisions", status_code=303)

    _lock_admin_job_target(db, "market-retention")

    existing = db.scalar(
        select(AdminJob).where(
            AdminJob.job_type == AdminJobType.market_retention.value,
            AdminJob.status.in_(_ACTIVE_ADMIN_JOB_STATUSES),
        )
    )
    if existing is not None:
        return RedirectResponse("/admin/operations?error=market_retention_in_progress", status_code=303)

    is_dry_run = bool(dry_run)
    job = AdminJob(
        job_type=AdminJobType.market_retention.value,
        status=AdminJobStatus.queued.value,
        params={"dry_run": is_dry_run, "max_active_revisions": max_active_revisions},
        target_label=("Retentie OPCOM (dry-run)" if is_dry_run else "Retentie OPCOM"),
        triggered_by_user_id=user.id,
    )
    db.add(job)
    db.flush()
    record_audit(
        db, action="market_retention_triggered", resource_type="admin_job", resource_id=str(job.id),
        actor_user_id=user.id, actor_label=user.email,
        metadata={"dry_run": is_dry_run, "max_active_revisions": max_active_revisions},
    )
    db.commit()

    if not _enqueue_admin_job(db, job, admin_market_retention_job_task, user):
        return RedirectResponse("/admin/operations?error=enqueue_failed", status_code=303)

    return RedirectResponse("/admin/operations", status_code=303)


@router.get("/operations/optimize-confirm")
def optimization_confirm(
    request: Request,
    station_id: uuid.UUID = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pasul de confirmare cerut de issue #47 inainte de reexecutare: arata
    exact ce inputuri va folosi rularea (versiunea de configuratie/preferinte
    curent PUBLICATA, modul shadow/live curent al statiei) si, daca exista,
    planul activ care ar fi inlocuit (`superseded`) de o rulare reusita --
    nu presupune nimic despre continutul viitor al planului (asta depinde de
    solver), doar despre inputurile deja cunoscute acum."""
    station = db.get(Station, station_id)
    if station is None:
        return RedirectResponse("/admin/operations?error=station_not_found", status_code=303)

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
    active_plan = _active_plan_for_station(db, station_id)

    context = {
        "station": station,
        "config": config,
        "preference": preference,
        "active_plan": active_plan,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/optimization_confirm.html", context)


@router.post("/operations/optimize/{station_id}", dependencies=[Depends(verify_csrf)])
def trigger_optimization(
    request: Request,
    station_id: uuid.UUID,
    confirmed: str | None = Form(None),
    reason: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.workers.tasks import admin_optimize_station_job_task

    station = db.get(Station, station_id)
    if station is None:
        return RedirectResponse("/admin/operations?error=station_not_found", status_code=303)

    active_plan = _active_plan_for_station(db, station_id)
    # Confirmarea explicita e ceruta STRICT cand exista un plan activ de inlocuit --
    # o statie fara plan activ inca (prima rulare) nu are nimic de pierdut, deci nu
    # blocam fluxul existent (scripturi/teste care apeleaza direct aceasta ruta).
    if active_plan is not None and not confirmed:
        return RedirectResponse("/admin/operations?error=optimization_confirmation_required", status_code=303)

    _lock_admin_job_target(db, f"optimization:{station_id}")
    existing = db.scalar(
        select(AdminJob).where(
            AdminJob.job_type == AdminJobType.optimization.value,
            AdminJob.status.in_(_ACTIVE_ADMIN_JOB_STATUSES),
            AdminJob.station_id == station_id,
        )
    )
    if existing is not None:
        return RedirectResponse("/admin/operations?error=optimization_in_progress", status_code=303)

    reason = (reason or "").strip() or None
    job = AdminJob(
        job_type=AdminJobType.optimization.value,
        status=AdminJobStatus.queued.value,
        params={"reason": reason} if reason else {},
        target_label=station.name,
        station_id=station_id,
        triggered_by_user_id=user.id,
    )
    db.add(job)
    db.flush()
    record_audit(
        db, action="optimization_triggered", resource_type="admin_job", resource_id=str(job.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station_id,
        metadata={"reason": reason, "superseded_plan_version": active_plan.version if active_plan else None},
    )
    db.commit()

    if not _enqueue_admin_job(db, job, admin_optimize_station_job_task, user):
        return RedirectResponse("/admin/operations?error=enqueue_failed", status_code=303)

    return RedirectResponse("/admin/operations", status_code=303)


@router.get("/operations/optimization-runs/{run_id}")
def optimization_run_detail(
    request: Request,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Tabelul explicabil cerut de issue #47: coloane cu unitati/conventie de
    semn explicite, rezumat in limbaj natural pe segmente (grupare de
    intervale consecutive cu aceeasi actiune si acelasi motiv dominant) si
    provenance/freshness pentru datele folosite la construirea rularii."""
    run = db.get(OptimizationRun, run_id)
    if run is None:
        return RedirectResponse("/admin/operations?error=optimization_run_not_found", status_code=303)

    station = db.get(Station, run.station_id)
    preference = db.get(PreferenceVersion, run.preference_version_id) if run.preference_version_id else None

    rows: list[optimization_view.IntervalRow] = []
    segments: list[optimization_view.PlanSegment] = []
    if run.plan is not None:
        rows = optimization_view.build_rows(
            run.plan.intervals,
            min_reserve_soc_percent=float(preference.min_reserve_soc_percent) if preference else None,
            max_normal_soc_percent=float(preference.max_normal_soc_percent) if preference else None,
            interval_hours=run.interval_minutes / 60.0,
        )
        segments = optimization_view.group_segments(rows)

    triggered_by_user = db.get(User, run.triggered_by_user_id) if run.triggered_by_user_id else None

    context = {
        "run": run,
        "station": station,
        "plan": run.plan,
        "rows": rows,
        "segments": segments,
        "triggered_by_user": triggered_by_user,
        "input_snapshot": run.input_snapshot or {},
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/optimization_run_detail.html", context)


@router.get("/audit")
def audit_log(
    request: Request,
    action: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(200)
    if action:
        stmt = stmt.where(AuditLog.action.ilike(f"%{action}%"))
    if actor:
        stmt = stmt.where(AuditLog.actor_label.ilike(f"%{actor}%"))
    entries = db.scalars(stmt).all()
    context = {"entries": entries, "filter_action": action or "", "filter_actor": actor or "", **build_nav_context(db, user)}
    return templates.TemplateResponse(request, "admin/audit.html", context)


# --- Flota completa de device-uri, cross-statie (dashboard admin) --------
#
# Spre deosebire de /devices/pending (doar enrollment neasociat) si
# /devices/assigned (doar active+asociate, cu formulare de transfer/factory
# reset), aceasta pagina arata TOATE device-urile -- orice status, asociate
# sau nu -- doar ca lista read-only cu online/offline, linked/unlinked,
# firmware si ultimul instantaneu de resurse (CPU/memorie/temperatura/disk)
# raportat de dispozitiv. Actiunile ramase specifice ramai in paginile lor.


@router.get("/devices")
def fleet_devices_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    stations = db.scalars(select(Station).order_by(Station.name)).all()
    stations_by_id = {s.id: s for s in stations}
    # Issue #168: "update disponibil" badge per device -- the current
    # non-terminal deployment (if any), keyed by device_id.
    from app.services import firmware_service

    active_deployments = db.scalars(
        select(FirmwareDeployment).where(FirmwareDeployment.status.in_(firmware_service.IN_FLIGHT_STATUSES))
    ).all()
    deployment_by_device_id = {d.device_id: d for d in active_deployments}
    context = {
        "devices": devices,
        "stations_by_id": stations_by_id,
        "deployment_by_device_id": deployment_by_device_id,
        "now": utcnow(),
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/devices_fleet.html", context)


# --- Enrollment automat: inventar device-uri neasociate (issue #16) ------
#
# Alocarea unui device enrollat este restransa la platform_admin (acelasi
# guard ca restul acestui router): un installation_uuid e doar o identitate
# DECLARATA de dispozitiv, fara nicio afiliere de organizatie -- expunerea
# inventarului neasociat catre admini de organizatie ar permite unei
# organizatii sa "vada"/revendice un device destinat altei organizatii,
# inainte de orice corelare umana in afara platformei (ex. instalatorul
# comunica installation_uuid-ul catre clientul corect). Vezi
# docs/LIMITATIONS.md pentru nota completa.


@router.get("/devices/pending")
def pending_devices_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pending = device_service.list_pending_devices(db)
    now = utcnow()
    stations = db.scalars(select(Station).order_by(Station.name)).all()
    context = {
        "pending_devices": pending,
        "now": now,
        "stations": stations,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/devices_pending.html", context)


@router.post("/devices/{device_id}/allocate", dependencies=[Depends(verify_csrf)])
def allocate_pending_device(
    request: Request,
    device_id: uuid.UUID,
    station_id: uuid.UUID = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = db.get(Device, device_id)
    station = db.get(Station, station_id)
    if device is None or station is None:
        return RedirectResponse("/admin/devices/pending", status_code=303)

    try:
        secret = device_service.allocate_device(db, device, station, user)
    except device_service.DeviceServiceError as exc:
        db.rollback()
        record_audit(
            db, action="device_allocation_failed", resource_type="device", resource_id=str(device.id),
            actor_user_id=user.id, actor_label=user.email, station_id=station.id,
            metadata={"reason": str(exc)}, outcome="failure",
        )
        db.commit()
        return RedirectResponse("/admin/devices/pending?error=1", status_code=303)

    record_audit(
        db, action="device_allocated", resource_type="device", resource_id=str(device.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        metadata={"installation_uuid": device.installation_uuid},
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "admin/device_allocated.html",
        {"device": device, "station": station, "credential_secret": secret, **build_nav_context(db, user)},
    )


@router.post("/devices/{device_id}/revoke-enrollment", dependencies=[Depends(verify_csrf)])
def revoke_pending_device(
    request: Request,
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = db.get(Device, device_id)
    if device is not None:
        device_service.revoke_device(db, device, reason=f"Enrollment revocat manual de {user.email}.")
        record_audit(
            db, action="device_enrollment_revoked", resource_type="device", resource_id=str(device.id),
            actor_user_id=user.id, actor_label=user.email,
            metadata={"installation_uuid": device.installation_uuid},
        )
        db.commit()
    return RedirectResponse("/admin/devices/pending", status_code=303)


# --- Device-uri asociate: transfer / factory reset (issue #44) -----------
#
# Ambele actiuni sunt strict platform_admin (acelasi guard ca restul acestui
# router) si deliberat cross-tenant-capabile -- un transfer poate muta un
# device intre statii ale unor organizatii DIFERITE (hardware revandut/
# reinstalat la alt client), un scenariu administrativ real, distinct de o
# preluare neautorizata initiata de un utilizator de organizatie. Fiecare
# actiune cere confirmarea explicita a serialului/installation_uuid afisat pe
# pagina (nu doar un dialog JS `confirm()`, usor de ocolit printr-un POST
# direct) si e inregistrata in audit.


@router.get("/devices/assigned")
def assigned_devices_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    devices = db.scalars(
        select(Device).where(Device.status == DeviceStatus.active.value, Device.station_id.isnot(None)).order_by(Device.name)
    ).all()
    stations = db.scalars(select(Station).order_by(Station.name)).all()
    stations_by_id = {s.id: s for s in stations}
    context = {
        "devices": devices,
        "stations": stations,
        "stations_by_id": stations_by_id,
        "now": utcnow(),
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/devices_assigned.html", context)


def _confirm_matches(device: Device, confirm_value: str) -> bool:
    expected = (device.serial_number or device.installation_uuid or "").strip().upper()
    return bool(expected) and confirm_value.strip().upper() == expected


@router.post("/devices/{device_id}/transfer", dependencies=[Depends(verify_csrf)])
def transfer_device(
    request: Request,
    device_id: uuid.UUID,
    target_station_id: uuid.UUID = Form(...),
    confirm_identifier: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = db.get(Device, device_id)
    target_station = db.get(Station, target_station_id)
    if device is None or target_station is None:
        return RedirectResponse("/admin/devices/assigned?error=1", status_code=303)

    if not _confirm_matches(device, confirm_identifier):
        record_audit(
            db, action="device_transfer_failed", resource_type="device", resource_id=str(device.id),
            actor_user_id=user.id, actor_label=user.email,
            metadata={"reason": "confirmation_mismatch"}, outcome="failure",
        )
        db.commit()
        return RedirectResponse("/admin/devices/assigned?error=confirm_mismatch", status_code=303)

    try:
        old_station_id, secret = device_service.transfer_device(db, device, target_station, user)
    except device_service.DeviceServiceError as exc:
        db.rollback()
        record_audit(
            db, action="device_transfer_failed", resource_type="device", resource_id=str(device.id),
            actor_user_id=user.id, actor_label=user.email,
            metadata={"reason": str(exc)}, outcome="failure",
        )
        db.commit()
        return RedirectResponse("/admin/devices/assigned?error=1", status_code=303)

    record_audit(
        db, action="device_transferred", resource_type="device", resource_id=str(device.id),
        actor_user_id=user.id, actor_label=user.email, station_id=target_station.id,
        metadata={"from_station_id": str(old_station_id), "to_station_id": str(target_station.id)},
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "admin/device_allocated.html",
        {
            "device": device, "station": target_station, "credential_secret": secret,
            "back_url": "/admin/devices/assigned", "back_label": "Inapoi la device-uri asociate",
            **build_nav_context(db, user),
        },
    )


@router.post("/devices/{device_id}/factory-reset", dependencies=[Depends(verify_csrf)])
def factory_reset_device(
    request: Request,
    device_id: uuid.UUID,
    confirm_identifier: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = db.get(Device, device_id)
    if device is None:
        return RedirectResponse("/admin/devices/assigned?error=1", status_code=303)

    if not _confirm_matches(device, confirm_identifier):
        record_audit(
            db, action="device_factory_reset_failed", resource_type="device", resource_id=str(device.id),
            actor_user_id=user.id, actor_label=user.email,
            metadata={"reason": "confirmation_mismatch"}, outcome="failure",
        )
        db.commit()
        return RedirectResponse("/admin/devices/assigned?error=confirm_mismatch", status_code=303)

    old_station_id = device.station_id
    try:
        device_service.factory_reset_device(db, device)
    except device_service.DeviceServiceError as exc:
        db.rollback()
        record_audit(
            db, action="device_factory_reset_failed", resource_type="device", resource_id=str(device.id),
            actor_user_id=user.id, actor_label=user.email,
            metadata={"reason": str(exc)}, outcome="failure",
        )
        db.commit()
        return RedirectResponse("/admin/devices/assigned?error=1", status_code=303)

    record_audit(
        db, action="device_factory_reset", resource_type="device", resource_id=str(device.id),
        actor_user_id=user.id, actor_label=user.email, station_id=old_station_id,
        metadata={"installation_uuid": device.installation_uuid},
    )
    db.commit()
    return RedirectResponse("/admin/devices/assigned?reset=1", status_code=303)
