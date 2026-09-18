"""UI conectare/deconectare cont Deye Cloud (issue #43) -- read-only, per
statie. Fiecare actiune de scriere (connect/select/disconnect) trece prin
`edit_access` (organization_admin+, acelasi prag ca gestiunea celorlalte
dispozitive -- vezi docs/RBAC_MATRIX.md); pagina de stare e vizibila oricui
are acces de citire la statie."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.database import get_db
from app.models.user import User
from app.services import deye_cloud_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
view_access = StationAccess()
edit_access = StationAccess(min_role="organization_admin")


def _redirect(station_id: uuid.UUID, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode

    params = {}
    if error:
        params["error"] = error
    if notice:
        params["notice"] = notice
    qs = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/stations/{station_id}/integrations/deye{qs}", status_code=303)


def _parse_station_datetime(value: str, station_timezone: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    tz = ZoneInfo(station_timezone)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


@router.get("/stations/{station_id}/integrations/deye")
def page(
    request: Request,
    db: Session = Depends(get_db),
    access=Depends(view_access),
    user: User = Depends(get_current_user),
):
    station, role = access
    connection = deye_cloud_service.get_connection_for_station(db, station.id)
    remote_stations: list[dict] = []
    if connection is not None and connection.status == "pending_selection":
        remote_stations = deye_cloud_service.list_pending_remote_stations(connection)

    context = {
        "station": station,
        "connection": connection,
        "remote_stations": remote_stations,
        "can_edit": role in ("organization_admin", "platform_admin"),
        "history_import_max_days": deye_cloud_service.HISTORY_IMPORT_MAX_WINDOW.days,
        **build_nav_context(db, user, station.id),
    }
    return templates.TemplateResponse(request, "stations/deye_integration.html", context)


@router.post("/stations/{station_id}/integrations/deye/connect", dependencies=[Depends(verify_csrf)])
def connect(
    app_id: str = Form(...),
    app_secret: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    consent: str | None = Form(None),
    db: Session = Depends(get_db),
    access=Depends(edit_access),
    user: User = Depends(get_current_user),
):
    station, _role = access
    if not consent:
        return _redirect(station.id, error="Trebuie sa confirmi consimtamantul explicit pentru import.")
    try:
        check_fixed_window(
            f"deye_cloud_connect:{user.id}:{station.id}",
            deye_cloud_service.settings.deye_cloud_connect_attempts_per_hour,
            3600,
        )
    except RateLimitExceeded:
        return _redirect(station.id, error="Prea multe incercari de conectare. Reincearca mai tarziu.")
    try:
        connection, _stations = deye_cloud_service.start_connection(
            db, station, user, app_id.strip(), app_secret, email.strip(), password
        )
        db.commit()
    except deye_cloud_service.DeyeCloudConfigError:
        db.rollback()
        return _redirect(station.id, error="Integrarea Deye Cloud necesita appId si appSecret pentru aceasta statie.")
    except deye_cloud_service.DeyeCloudAuthError:
        db.rollback()
        return _redirect(station.id, error="Autentificare Deye Cloud esuata -- verifica email-ul si parola.")
    except deye_cloud_service.DeyeCloudError:
        db.rollback()
        return _redirect(station.id, error="Deye Cloud este indisponibil momentan. Reincearca mai tarziu.")

    record_audit(
        db, action="deye_cloud_connected", resource_type="deye_cloud_connection", resource_id=str(connection.id),
        actor_user_id=user.id, actor_label="user", station_id=station.id,
        metadata={"account_email": email.strip(), "app_id": app_id.strip()},
    )
    db.commit()
    return _redirect(station.id, notice="Autentificare reusita. Alege statia din contul tau Deye Cloud.")


@router.post("/stations/{station_id}/integrations/deye/select", dependencies=[Depends(verify_csrf)])
def select_station(
    remote_station_id: int = Form(...),
    db: Session = Depends(get_db),
    access=Depends(edit_access),
    user: User = Depends(get_current_user),
):
    station, _role = access
    connection = deye_cloud_service.get_connection_for_station(db, station.id)
    if connection is None or connection.status != "pending_selection":
        return _redirect(station.id, error="Nicio conectare Deye Cloud in asteptare de selectie.")

    try:
        deye_cloud_service.select_remote_station(db, connection, remote_station_id)
    except deye_cloud_service.DeyeCloudError:
        db.rollback()
        return _redirect(station.id, error="Nu am putut incarca statia selectata din Deye Cloud.")

    record_audit(
        db, action="deye_cloud_station_selected", resource_type="deye_cloud_connection", resource_id=str(connection.id),
        actor_user_id=user.id, actor_label="user", station_id=station.id,
        metadata={"remote_station_id": remote_station_id},
    )
    db.commit()
    return _redirect(station.id, notice="Statia Deye Cloud a fost legata. Telemetria va aparea dupa urmatorul polling.")


@router.post("/stations/{station_id}/integrations/deye/import-history", dependencies=[Depends(verify_csrf)])
def import_history(
    start_at: str = Form(...),
    end_at: str = Form(...),
    db: Session = Depends(get_db),
    access=Depends(edit_access),
    user: User = Depends(get_current_user),
):
    station, _role = access
    connection = deye_cloud_service.get_connection_for_station(db, station.id)
    if connection is None:
        return _redirect(station.id, error="Nicio conectare Deye Cloud activa pentru import istoric.")

    try:
        start = _parse_station_datetime(start_at, station.timezone)
        end = _parse_station_datetime(end_at, station.timezone)
        result = deye_cloud_service.import_station_history(db, connection, start, end)
    except ValueError as exc:
        db.rollback()
        return _redirect(station.id, error=str(exc))
    except deye_cloud_service.DeyeCloudConfigError as exc:
        db.rollback()
        return _redirect(station.id, error=str(exc))
    except deye_cloud_service.DeyeCloudAuthError:
        db.rollback()
        return _redirect(station.id, error="Autentificare Deye Cloud esuata -- reconecteaza contul.")
    except deye_cloud_service.DeyeCloudError:
        db.rollback()
        return _redirect(station.id, error="Nu am putut importa istoricul din Deye Cloud. Reincearca mai tarziu.")

    record_audit(
        db, action="deye_cloud_history_imported", resource_type="deye_cloud_connection", resource_id=str(connection.id),
        actor_user_id=user.id, actor_label="user", station_id=station.id,
        metadata={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "created": result["created"],
            "skipped_existing": result["skipped_existing"],
            "skipped_invalid_timestamp": result["skipped_invalid_timestamp"],
            "implausible_power_rows": result["implausible_power_rows"],
        },
    )
    db.commit()
    notice = f"Import istoric finalizat: {result['created']} randuri noi, {result['skipped_existing']} deja existente."
    return _redirect(station.id, notice=notice)


@router.post("/stations/{station_id}/integrations/deye/disconnect", dependencies=[Depends(verify_csrf)])
def disconnect(
    db: Session = Depends(get_db),
    access=Depends(edit_access),
    user: User = Depends(get_current_user),
):
    station, _role = access
    connection = deye_cloud_service.get_connection_for_station(db, station.id)
    if connection is None:
        return _redirect(station.id, error="Nicio conectare Deye Cloud de deconectat.")

    deye_cloud_service.disconnect(db, connection, user)
    record_audit(
        db, action="deye_cloud_disconnected", resource_type="deye_cloud_connection", resource_id=str(connection.id),
        actor_user_id=user.id, actor_label="user", station_id=station.id,
    )
    db.commit()
    return _redirect(station.id, notice="Contul Deye Cloud a fost deconectat. Telemetria importata anterior ramane vizibila ca istoric.")
