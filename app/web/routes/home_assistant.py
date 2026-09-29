from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.database import get_db
from app.models.user import User
from app.schemas.home_assistant import KINDS, ConnectionInput, MappingInput
from app.services import home_assistant_service as service
from app.services.home_assistant_config import package_yaml
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
view_access = StationAccess()
edit_access = StationAccess(min_role="organization_admin")


def _page(request, db, station, role, user, error=None, status_code=200):
    settings = get_settings()
    connection = service.get_connection(db, station.id)
    mappings = service.mappings_for(db, connection)
    context = {
        **build_nav_context(db, user, station.id),
        "station": station,
        "connection": connection,
        "mappings": mappings,
        "observations": [
            service.observation(
                m, enabled=bool(settings.home_assistant_mqtt_enabled and connection.enabled)
            )
            for m in mappings
        ],
        "brokers": settings.home_assistant_mqtt_brokers
        if settings.home_assistant_mqtt_enabled
        else {},
        "enabled_globally": settings.home_assistant_mqtt_enabled,
        "can_edit": role in ("organization_admin", "platform_admin"),
        "kinds": KINDS,
        "error": error,
        "status_labels": service.STATUS_LABELS,
        "error_labels": service.ERROR_LABELS,
        "topic_root": service.topic_root(connection) if connection else None,
    }
    return templates.TemplateResponse(
        request,
        "stations/home_assistant.html",
        context,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stations/{station_id}/integrations/home-assistant")
def page(
    request: Request,
    db: Session = Depends(get_db),
    access=Depends(view_access),
    user: User = Depends(get_current_user),
):
    return _page(request, db, *access, user)


@router.get("/stations/{station_id}/integrations/home-assistant/status")
def status(db: Session = Depends(get_db), access=Depends(view_access)):
    station, _ = access
    connection = service.get_connection(db, station.id)
    enabled = bool(get_settings().home_assistant_mqtt_enabled and connection and connection.enabled)
    return JSONResponse(
        jsonable_encoder(
            {
                "schema_version": 1,
                "enabled": enabled,
                "status": connection.status if connection else "not_configured",
                "error_code": connection.error_code if connection else None,
                "last_connected_at": connection.last_connected_at if connection else None,
                "next_attempt_at": connection.next_attempt_at if connection else None,
                "observations": [
                    service.observation(m, enabled=enabled)
                    for m in service.mappings_for(db, connection)
                ],
                "physical_control": False,
            }
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stations/{station_id}/integrations/home-assistant/home-assistant.yaml")
def configuration_file(db: Session = Depends(get_db), access=Depends(edit_access)):
    station, _ = access
    connection = service.get_connection(db, station.id)
    if not connection or not connection.enabled:
        raise HTTPException(404, "Configureaza mai intai integrarea.")
    return Response(
        package_yaml(connection, service.mappings_for(db, connection)),
        media_type="application/yaml",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="ems-home-assistant.yaml"',
        },
    )


@router.post(
    "/stations/{station_id}/integrations/home-assistant/configure",
    dependencies=[Depends(verify_csrf)],
)
async def configure(
    request: Request,
    db: Session = Depends(get_db),
    access=Depends(edit_access),
    user: User = Depends(get_current_user),
):
    station, role = access
    form = await request.form()
    try:
        entities = form.getlist("entity_id")
        kinds, units, ages = (
            form.getlist("kind"),
            form.getlist("unit"),
            form.getlist("max_age_seconds"),
        )
        if not 1 <= len(entities) <= 20 or not len(entities) == len(kinds) == len(units) == len(
            ages
        ):
            raise ValueError("Alege intre 1 si 20 de senzori, cu tip, unitate si prag de vechime.")
        data = ConnectionInput(
            revision=form.get("revision", "0"),
            broker_key=form.get("broker_key", ""),
            username=form.get("username", ""),
            password=form.get("password", ""),
            consent=form.get("consent") == "yes",
            publish_consent=form.get("publish_consent") == "yes",
            mappings=[
                MappingInput(entity_id=e, kind=k, unit=u, max_age_seconds=a)
                for e, k, u, a in zip(entities, kinds, units, ages, strict=True)
            ],
        )
        service.configure(db, station, user, data)
        db.commit()
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = (
            "Verifica senzorii, unitatile, credentialele si consimtamantul. Sunt permise doar datele agregate si tipurile listate."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return _page(request, db, station, role, user, error=error, status_code=422)
    return RedirectResponse(
        f"/stations/{station.id}/integrations/home-assistant?saved=1", status_code=303
    )


@router.post(
    "/stations/{station_id}/integrations/home-assistant/manage", dependencies=[Depends(verify_csrf)]
)
def manage(
    request: Request,
    action: str = Form(...),
    revision: int = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
    access=Depends(edit_access),
    user: User = Depends(get_current_user),
):
    station, role = access
    try:
        if action in ("test", "rotate"):
            check_fixed_window(f"ha_test:{station.id}", 10, 3600)
        service.manage(db, station, user, action, revision, username=username, password=password)
        db.commit()
    except RateLimitExceeded:
        return _page(
            request,
            db,
            station,
            role,
            user,
            error="Prea multe teste. Reincearca mai tarziu.",
            status_code=429,
        )
    except ValueError as exc:
        db.rollback()
        return _page(request, db, station, role, user, error=str(exc), status_code=422)
    return RedirectResponse(f"/stations/{station.id}/integrations/home-assistant", status_code=303)
