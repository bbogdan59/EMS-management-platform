from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.config import get_settings
from app.database import get_db
from app.services import home_assistant_bridge as service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
access = StationAccess()


def bridge_snapshot(db, station):
    bridge = service.get_bridge(db, station.id)
    active = get_settings().home_assistant_bridge_enabled and (
        bridge is None or service.owner_active(db, bridge)
    )
    return service.snapshot(bridge, enabled=active)


@router.get("/stations/{station_id}/integrations/home-assistant-bridge/status")
def status(
    request: Request,
    db: Session = Depends(get_db),
    station_access=Depends(access),
):
    station, _ = station_access
    return templates.TemplateResponse(
        request,
        "partials/_home_assistant_bridge_sensors.html",
        {"station": station, "bridge": bridge_snapshot(db, station)},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stations/{station_id}/integrations/home-assistant-bridge")
def page(
    request: Request,
    db: Session = Depends(get_db),
    station_access=Depends(access),
    user=Depends(get_current_user),
):
    station, role = station_access
    enabled = get_settings().home_assistant_bridge_enabled
    return templates.TemplateResponse(
        request,
        "stations/home_assistant_bridge.html",
        {
            **build_nav_context(db, user, station.id),
            "station": station,
            "bridge": bridge_snapshot(db, station),
            "bridge_enabled": enabled,
            "can_edit": role in ("platform_admin", "organization_admin"),
        },
        headers={"Cache-Control": "no-store"},
    )
