from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_platform_admin
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.security import utcnow
from app.database import get_db
from app.models.admin_job import AdminJob
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.command import Command
from app.models.device import Device
from app.models.enums import AdminJobStatus, AdminJobType, AlertStatus
from app.models.market import ImportRun
from app.models.optimization import OptimizationRun
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import User
from app.services import device_service, station_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_platform_admin)])


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
    context = {"rows": rows, **build_nav_context(db, user)}
    return templates.TemplateResponse(request, "admin/users.html", context)


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
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/operations.html", context)


_ACTIVE_ADMIN_JOB_STATUSES = (AdminJobStatus.queued.value, AdminJobStatus.running.value)


@router.post("/operations/import-opcom", dependencies=[Depends(verify_csrf)])
def trigger_opcom_import(
    request: Request,
    delivery_date: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from datetime import date as date_cls

    from app.workers.tasks import admin_opcom_import_job_task

    d = date_cls.fromisoformat(delivery_date).isoformat()

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

    async_result = admin_opcom_import_job_task.delay(str(job.id))
    job.celery_task_id = async_result.id
    db.commit()

    return RedirectResponse("/admin/operations", status_code=303)


@router.post("/operations/optimize/{station_id}", dependencies=[Depends(verify_csrf)])
def trigger_optimization(
    request: Request,
    station_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.workers.tasks import admin_optimize_station_job_task

    station = db.get(Station, station_id)
    if station is None:
        return RedirectResponse("/admin/operations?error=station_not_found", status_code=303)

    existing = db.scalar(
        select(AdminJob).where(
            AdminJob.job_type == AdminJobType.optimization.value,
            AdminJob.status.in_(_ACTIVE_ADMIN_JOB_STATUSES),
            AdminJob.station_id == station_id,
        )
    )
    if existing is not None:
        return RedirectResponse("/admin/operations?error=optimization_in_progress", status_code=303)

    job = AdminJob(
        job_type=AdminJobType.optimization.value,
        status=AdminJobStatus.queued.value,
        params={},
        target_label=station.name,
        station_id=station_id,
        triggered_by_user_id=user.id,
    )
    db.add(job)
    db.flush()
    record_audit(
        db, action="optimization_triggered", resource_type="admin_job", resource_id=str(job.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station_id,
    )
    db.commit()

    async_result = admin_optimize_station_job_task.delay(str(job.id))
    job.celery_task_id = async_result.id
    db.commit()

    return RedirectResponse("/admin/operations", status_code=303)


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
