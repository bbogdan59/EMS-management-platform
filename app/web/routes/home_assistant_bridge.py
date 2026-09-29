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


@router.get("/stations/{station_id}/integrations/home-assistant-bridge")
def page(
    request: Request,
    db: Session = Depends(get_db),
    station_access=Depends(access),
    user=Depends(get_current_user),
):
    station, role = station_access
    bridge = service.get_bridge(db, station.id)
    enabled = get_settings().home_assistant_bridge_enabled
    return templates.TemplateResponse(
        request,
        "stations/home_assistant_bridge.html",
        {
            **build_nav_context(db, user, station.id),
            "station": station,
            "bridge": service.snapshot(
                bridge, enabled=enabled and (bridge is None or service.owner_active(db, bridge))
            ),
            "bridge_enabled": enabled,
            "can_edit": role in ("platform_admin", "organization_admin"),
        },
        headers={"Cache-Control": "no-store"},
    )
