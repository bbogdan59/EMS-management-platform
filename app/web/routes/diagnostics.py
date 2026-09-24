from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.deps import OrganizationAccess, StationAccess, get_current_user
from app.config import get_settings
from app.core.audit import record_audit
from app.core.crypto import encrypt_secret
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.core.security import utcnow
from app.database import get_db
from app.models.alert import Alert
from app.models.health import AlertEvent, DiagnosticGrant
from app.models.notification import Notification, NotificationDelivery
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import User
from app.services import energy_assistant_service as assistant
from app.services import fleet_diagnostics_service as fleet
from app.services import health_service as health
from app.services import notification_service as notifications
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
station_viewer = StationAccess("viewer")
station_operator = StationAccess("operator")
station_admin = StationAccess("organization_admin")
organization_viewer = OrganizationAccess("viewer")


def _limit(key, count=20, seconds=3600):
    try:
        check_fixed_window(key, count, seconds)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            "Prea multe cereri. Reincearca mai tarziu.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc


def _error(exc):
    raise HTTPException(400, str(exc)) from exc


@router.get("/fleet/health")
def fleet_health(
    request: Request,
    online: str = "",
    severity: str = "",
    firmware: str = "",
    quality: str = "",
    fault: str = "",
    update_state: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stations = db.scalars(fleet.scoped_stations(db, user)).all()
    rows = [fleet.health_summary(db, s) for s in stations]
    rows = [
        r
        for r in rows
        if (not online or r["online"] == (online == "online"))
        and (not severity or r["severity"] == severity)
        and (not firmware or firmware in r["firmware"])
        and (not quality or r["quality"] == quality)
        and (not fault or r["fault"] == (fault == "yes"))
        and (not update_state or r["update_state"] == update_state)
    ]
    return templates.TemplateResponse(
        request,
        "diagnostics/fleet.html",
        {
            **build_nav_context(db, user),
            "rows": rows[offset : offset + 100],
            "total": len(rows),
            "offset": offset,
        },
    )


@router.get("/stations/{station_id}/health")
def station_health(
    station_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    station, role = fleet.diagnostic_access(db, user, station_id)
    alerts = db.scalars(
        select(Alert)
        .where(Alert.station_id == station.id)
        .order_by(Alert.created_at.desc())
        .limit(100)
    ).all()
    events = db.scalars(
        select(AlertEvent)
        .where(AlertEvent.alert_id.in_([a.id for a in alerts]))
        .order_by(AlertEvent.occurred_at)
    ).all()
    end = utcnow()
    grants = (
        db.scalars(select(DiagnosticGrant).where(DiagnosticGrant.station_id == station_id)).all()
        if role in ("organization_admin", "platform_admin")
        else []
    )
    return templates.TemplateResponse(
        request,
        "diagnostics/station.html",
        {
            **build_nav_context(db, user, station_id),
            "station": station,
            "role": role,
            "alerts": alerts,
            "events": events,
            "summary": fleet.health_summary(db, station),
            "diagnostics": fleet.latest_diagnostics(db, station),
            "rules": health.RULES,
            "sanitize": fleet.sanitized_evidence,
            "grants": grants,
            "pack": fleet.escalation_pack(db, station, end - timedelta(days=1), end),
            "assistant_enabled": get_settings().energy_assistant_enabled,
        },
    )


@router.post("/stations/{station_id}/health/{alert_id}", dependencies=[Depends(verify_csrf)])
def alert_feedback(
    station_id: uuid.UUID,
    alert_id: uuid.UUID,
    action: str = Form(...),
    reason: str = Form(..., min_length=1, max_length=500),
    access=Depends(station_operator),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        health.feedback(db, access[0], alert_id, action, reason, user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        _error(exc)
    db.commit()
    return RedirectResponse(f"/stations/{station_id}/health#alert-{alert_id}", 303)


@router.post("/stations/{station_id}/diagnostic-grants", dependencies=[Depends(verify_csrf)])
def diagnostic_grant(
    station_id: uuid.UUID,
    user_id: uuid.UUID = Form(...),
    expires_at: datetime = Form(...),
    revoke: bool = Form(False),
    access=Depends(station_admin),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        fleet.grant_access(db, user, access[0], user_id, expires_at, revoke=revoke)
    except ValueError as exc:
        _error(exc)
    db.commit()
    return RedirectResponse(f"/stations/{station_id}/health", 303)


@router.get("/stations/{station_id}/diagnostics/export")
def diagnostics_export(
    station_id: uuid.UUID,
    start: datetime,
    end: datetime,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    station, _ = fleet.diagnostic_access(db, user, station_id)
    _limit(f"diagnostic-export:{user.id}", 30)
    try:
        pack = fleet.escalation_pack(db, station, start, end)
    except ValueError as exc:
        _error(exc)
    record_audit(
        db,
        action="diagnostic.export",
        resource_type="station",
        resource_id=str(station.id),
        actor_user_id=user.id,
        station_id=station.id,
        organization_id=station.organization_id,
        metadata={"start": start.isoformat(), "end": end.isoformat()},
    )
    db.commit()
    return Response(
        json.dumps(pack, sort_keys=True, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="diagnostics-{station_id}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/health/runbooks")
def runbooks(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return templates.TemplateResponse(
        request, "diagnostics/runbooks.html", {**build_nav_context(db, user), "rules": health.RULES}
    )


@router.get("/notifications")
def notification_list(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    orgs = db.scalars(
        select(Organization)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Membership.user_id == user.id,
            Membership.is_active.is_(True),
            Organization.status != "archived",
        )
        .order_by(Organization.name)
    ).all()
    org_ids = [o.id for o in orgs]
    notices = db.scalars(
        select(Notification)
        .join(Station)
        .where(Notification.user_id == user.id, Station.organization_id.in_(org_ids))
        .order_by(Notification.created_at.desc())
        .limit(100)
    ).all()
    prefs = {o.id: notifications.preference(db, user, o) for o in orgs}
    deliveries = db.scalars(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.user_id == user.id,
            NotificationDelivery.organization_id.in_(org_ids),
        )
        .order_by(NotificationDelivery.created_at.desc())
        .limit(50)
    ).all()
    db.commit()
    return templates.TemplateResponse(
        request,
        "diagnostics/notifications.html",
        {
            **build_nav_context(db, user),
            "notices": notices,
            "orgs": orgs,
            "prefs": prefs,
            "deliveries": deliveries,
            "categories": notifications.CATEGORIES,
            "severities": notifications.SEVERITIES,
            "channels": notifications.CHANNELS,
            "modes": notifications.MODES,
            "settings": get_settings(),
        },
    )


@router.post("/notifications/{notice_id}/read", dependencies=[Depends(verify_csrf)])
def mark_read(
    notice_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    notice = db.scalar(
        select(Notification)
        .where(Notification.id == notice_id, Notification.user_id == user.id)
        .with_for_update()
    )
    if not notice:
        raise HTTPException(404, "Notificare inexistenta.")
    StationAccess("viewer")(station_id=notice.station_id, db=db, user=user)
    notice.read_at = notice.read_at or utcnow()
    db.commit()
    return RedirectResponse("/notifications", 303)


@router.post(
    "/organizations/{organization_id}/notification-preferences", dependencies=[Depends(verify_csrf)]
)
async def notification_preferences(
    organization_id: uuid.UUID,
    request: Request,
    timezone: str = Form(...),
    quiet_start: int = Form(22, ge=0, le=23),
    quiet_end: int = Form(8, ge=0, le=23),
    escalation_minutes: int = Form(15, ge=0, le=1440),
    weekly_report: bool = Form(False),
    unsubscribe: bool = Form(False),
    access=Depends(organization_viewer),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(400, "Fus orar invalid.") from exc
    form = await request.form()
    matrix = {key[7:]: value for key, value in form.items() if key.startswith("matrix:")}
    try:
        notifications.validate_matrix(matrix)
    except ValueError as exc:
        _error(exc)
    pref = notifications.preference(db, user, access[0], lock=True)
    pref.timezone, pref.quiet_start, pref.quiet_end = timezone, quiet_start, quiet_end
    pref.escalation_minutes, pref.weekly_report, pref.matrix = (
        escalation_minutes,
        weekly_report,
        matrix,
    )
    if unsubscribe:
        pref.matrix, pref.weekly_report, pref.encrypted_push_subscription = {}, False, None
        pref.verification_hash, pref.verification_expires_at = None, None
    record_audit(
        db,
        action="notification.preferences",
        resource_type="organization",
        resource_id=str(organization_id),
        actor_user_id=user.id,
        organization_id=organization_id,
        metadata={"unsubscribe": unsubscribe},
    )
    db.commit()
    return RedirectResponse("/notifications", 303)


@router.post(
    "/organizations/{organization_id}/notifications/verify", dependencies=[Depends(verify_csrf)]
)
def email_verify(
    organization_id: uuid.UUID,
    code: str = Form(""),
    access=Depends(organization_viewer),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _limit(f"notification-verify:{user.id}", 5, 900)
    pref = notifications.preference(db, user, access[0], lock=True)
    try:
        if code:
            notifications.verify_email(pref, user, code)
        else:
            notifications.request_verification(db, pref, user)
    except ValueError as exc:
        _error(exc)
    db.commit()
    return RedirectResponse("/notifications", 303)


@router.post(
    "/organizations/{organization_id}/notifications/push", dependencies=[Depends(verify_csrf)]
)
async def push_subscribe(
    organization_id: uuid.UUID,
    request: Request,
    access=Depends(organization_viewer),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not get_settings().notifications_push_enabled:
        raise HTTPException(409, "Push nu este configurat.")
    _limit(f"notification-push:{user.id}", 10)
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(413, "Subscriptie prea mare.")
    try:
        subscription = notifications.validate_subscription(json.loads(body))
    except (ValueError, TypeError) as exc:
        _error(exc)
    pref = notifications.preference(db, user, access[0], lock=True)
    pref.encrypted_push_subscription = encrypt_secret(json.dumps(subscription))
    db.commit()
    return {"status": "subscribed"}


@router.get("/notifications-worker.js", include_in_schema=False)
def notification_worker():
    return FileResponse(
        Path(__file__).parents[1] / "static/js/notifications-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/stations/{station_id}/assistant", dependencies=[Depends(verify_csrf)])
def energy_assistant(
    station_id: uuid.UUID,
    question: str = Form(..., min_length=1, max_length=1000),
    day: date | None = Form(None),
    access=Depends(station_viewer),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not get_settings().energy_assistant_enabled:
        raise HTTPException(404, "Asistentul nu este activat.")
    _limit(f"energy-assistant:{user.id}")
    try:
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        result = assistant.answer(db, user, station_id, question, day)
        db.commit()
    except ValueError as exc:
        _error(exc)
    except DBAPIError:
        db.rollback()
        result = {
            "status": "unavailable",
            "answer": "Datele nu pot fi consultate acum. Reincearca mai tarziu.",
            "evidence": [],
            "generated": True,
        }
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
