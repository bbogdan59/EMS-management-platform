"""Backoffice pentru inventarul/OTA firmware -- issue #168. Router separat
de `admin.py` (deja mare), montat cu acelasi prefix `/admin` si aceeasi
protectie `require_platform_admin`; publicarea/revocarea unui release si
pornirea unui rollout raman strict capabilitati de platforma."""
from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_platform_admin
from app.config import get_settings
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.database import get_db
from app.models.device import Device
from app.models.firmware import FirmwareDeployment, FirmwareRelease, FirmwareRollout
from app.models.user import User
from app.services import firmware_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_platform_admin)])


def _redirect(path: str, **errors) -> RedirectResponse:
    query = urlencode(errors)
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


@router.get("/firmware/releases")
def releases_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    releases = db.scalars(select(FirmwareRelease).order_by(FirmwareRelease.created_at.desc())).all()
    context = {
        "releases": releases,
        "errors": request.query_params.getlist("error"),
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/firmware_releases.html", context)


@router.post("/firmware/releases", dependencies=[Depends(verify_csrf)])
async def create_release(
    request: Request,
    version: str = Form(...),
    channel: str = Form(...),
    hardware_platform: str = Form(...),
    architecture: str = Form(...),
    protocol_schema_version: int = Form(...),
    min_compatible_agent_version: str = Form(""),
    release_notes: str = Form(""),
    artifact: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    settings = get_settings()
    raw = await artifact.read(settings.firmware_max_artifact_bytes + 1)
    if len(raw) > settings.firmware_max_artifact_bytes:
        return _redirect("/admin/firmware/releases", error="artifact_too_large")
    try:
        release = firmware_service.create_release(
            db, user, version=version, channel=channel, hardware_platform=hardware_platform,
            architecture=architecture, protocol_schema_version=protocol_schema_version,
            artifact_bytes=raw, min_compatible_agent_version=(min_compatible_agent_version or None),
            release_notes=(release_notes or None),
        )
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect("/admin/firmware/releases", error=str(exc))
    record_audit(
        db, action="firmware_release_created", resource_type="firmware_release", resource_id=str(release.id),
        actor_user_id=user.id, actor_label=user.email, metadata={"version": release.version, "channel": release.channel},
    )
    db.commit()
    return _redirect("/admin/firmware/releases")


@router.post("/firmware/releases/{release_id}/publish", dependencies=[Depends(verify_csrf)])
def publish_release(
    release_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    release = db.get(FirmwareRelease, release_id)
    if release is None:
        return _redirect("/admin/firmware/releases", error="not_found")
    try:
        firmware_service.publish_release(db, release)
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect("/admin/firmware/releases", error=str(exc))
    record_audit(
        db, action="firmware_release_published", resource_type="firmware_release", resource_id=str(release.id),
        actor_user_id=user.id, actor_label=user.email, metadata={"version": release.version},
    )
    db.commit()
    return _redirect("/admin/firmware/releases")


@router.post("/firmware/releases/{release_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke_release(
    release_id: uuid.UUID, reason: str = Form(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    release = db.get(FirmwareRelease, release_id)
    if release is None:
        return _redirect("/admin/firmware/releases", error="not_found")
    try:
        firmware_service.revoke_release(db, release, reason=reason)
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect("/admin/firmware/releases", error=str(exc))
    record_audit(
        db, action="firmware_release_revoked", resource_type="firmware_release", resource_id=str(release.id),
        actor_user_id=user.id, actor_label=user.email, metadata={"version": release.version, "reason": reason},
    )
    db.commit()
    return _redirect("/admin/firmware/releases")


@router.get("/firmware/rollouts")
def rollouts_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rollouts = db.scalars(select(FirmwareRollout).order_by(FirmwareRollout.created_at.desc())).all()
    releases_by_id = {r.id: r for r in db.scalars(select(FirmwareRelease)).all()}
    published_releases = [r for r in releases_by_id.values() if r.status == "published"]
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    context = {
        "rollouts": rollouts,
        "releases_by_id": releases_by_id,
        "published_releases": sorted(published_releases, key=lambda r: r.version),
        "devices": devices,
        "errors": request.query_params.getlist("error"),
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/firmware_rollouts.html", context)


@router.post("/firmware/rollouts/preview", dependencies=[Depends(verify_csrf)])
def preview_rollout(
    request: Request,
    release_id: uuid.UUID = Form(...),
    device_id: list[uuid.UUID] = Form(...),
    max_concurrent: int = Form(5),
    failure_threshold_percent: int = Form(20),
    allow_downgrade: str | None = Form(None),
    downgrade_reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Issue #168: shows the confirmation summary (device count, current vs.
    target versions, incompatibilities) BEFORE anything is persisted -- the
    form on this page resubmits the same selection to the actual create
    route below."""
    release = db.get(FirmwareRelease, release_id)
    if release is None or release.status != "published":
        return _redirect("/admin/firmware/rollouts", error="release_not_publishable")
    devices = [d for d in (db.get(Device, did) for did in device_id) if d is not None]
    preview = firmware_service.preview_rollout(release, devices)
    devices_by_id = {str(d.id): d for d in devices}
    context = {
        "release": release,
        "preview": preview,
        "devices_by_id": devices_by_id,
        "device_ids": device_id,
        "max_concurrent": max_concurrent,
        "failure_threshold_percent": failure_threshold_percent,
        "allow_downgrade": bool(allow_downgrade),
        "downgrade_reason": downgrade_reason,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/firmware_rollout_confirm.html", context)


@router.post("/firmware/rollouts", dependencies=[Depends(verify_csrf)])
def create_rollout(
    release_id: uuid.UUID = Form(...),
    device_id: list[uuid.UUID] = Form(...),
    max_concurrent: int = Form(5),
    failure_threshold_percent: int = Form(20),
    allow_downgrade: str | None = Form(None),
    downgrade_reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    release = db.get(FirmwareRelease, release_id)
    if release is None:
        return _redirect("/admin/firmware/rollouts", error="not_found")
    devices = [d for d in (db.get(Device, did) for did in device_id) if d is not None]
    try:
        result = firmware_service.create_rollout(
            db, user, release, devices, max_concurrent=max_concurrent,
            failure_threshold_percent=failure_threshold_percent, allow_downgrade=bool(allow_downgrade),
            downgrade_reason=(downgrade_reason or None),
        )
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect("/admin/firmware/rollouts", error=str(exc))
    record_audit(
        db, action="firmware_rollout_created", resource_type="firmware_rollout", resource_id=str(result["rollout"].id),
        actor_user_id=user.id, actor_label=user.email,
        metadata={"release_version": release.version, "created": len(result["created"]), "skipped": result["skipped"]},
    )
    db.commit()
    return _redirect(f"/admin/firmware/rollouts/{result['rollout'].id}")


@router.get("/firmware/rollouts/{rollout_id}")
def rollout_detail(
    rollout_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    rollout = db.get(FirmwareRollout, rollout_id)
    if rollout is None:
        return _redirect("/admin/firmware/rollouts", error="not_found")
    deployments = db.scalars(
        select(FirmwareDeployment).where(FirmwareDeployment.rollout_id == rollout.id).order_by(FirmwareDeployment.requested_at)
    ).all()
    release = db.get(FirmwareRelease, rollout.release_id)
    devices_by_id = {d.id: d for d in db.scalars(select(Device)).all()}
    context = {
        "rollout": rollout,
        "release": release,
        "deployments": deployments,
        "devices_by_id": devices_by_id,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/firmware_rollout_detail.html", context)


@router.post("/firmware/rollouts/{rollout_id}/pause", dependencies=[Depends(verify_csrf)])
def pause_rollout(rollout_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rollout = db.get(FirmwareRollout, rollout_id)
    if rollout is None:
        return _redirect("/admin/firmware/rollouts", error="not_found")
    try:
        firmware_service.pause_rollout(db, rollout)
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect(f"/admin/firmware/rollouts/{rollout_id}", error=str(exc))
    record_audit(
        db, action="firmware_rollout_paused", resource_type="firmware_rollout", resource_id=str(rollout.id),
        actor_user_id=user.id, actor_label=user.email,
    )
    db.commit()
    return _redirect(f"/admin/firmware/rollouts/{rollout_id}")


@router.post("/firmware/rollouts/{rollout_id}/resume", dependencies=[Depends(verify_csrf)])
def resume_rollout(rollout_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rollout = db.get(FirmwareRollout, rollout_id)
    if rollout is None:
        return _redirect("/admin/firmware/rollouts", error="not_found")
    try:
        firmware_service.resume_rollout(db, rollout)
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect(f"/admin/firmware/rollouts/{rollout_id}", error=str(exc))
    record_audit(
        db, action="firmware_rollout_resumed", resource_type="firmware_rollout", resource_id=str(rollout.id),
        actor_user_id=user.id, actor_label=user.email,
    )
    db.commit()
    return _redirect(f"/admin/firmware/rollouts/{rollout_id}")


@router.post("/firmware/rollouts/{rollout_id}/cancel", dependencies=[Depends(verify_csrf)])
def cancel_rollout(
    rollout_id: uuid.UUID, reason: str = Form(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    rollout = db.get(FirmwareRollout, rollout_id)
    if rollout is None:
        return _redirect("/admin/firmware/rollouts", error="not_found")
    try:
        firmware_service.cancel_rollout(db, rollout, reason=reason)
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        return _redirect(f"/admin/firmware/rollouts/{rollout_id}", error=str(exc))
    record_audit(
        db, action="firmware_rollout_cancelled", resource_type="firmware_rollout", resource_id=str(rollout.id),
        actor_user_id=user.id, actor_label=user.email, metadata={"reason": reason},
    )
    db.commit()
    return _redirect(f"/admin/firmware/rollouts/{rollout_id}")
