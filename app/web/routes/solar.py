from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.database import get_db
from app.models.solar import SolarInverter, SolarTracker
from app.schemas.solar import SolarHistory, SolarSnapshot, TrackerConfiguration
from app.services import solar_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
viewer = StationAccess("viewer")
operator = StationAccess("operator")


@router.get("/stations/{station_id}/solar")
def page(
    request: Request,
    access=Depends(viewer),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "solar/detail.html",
        {
            **build_nav_context(db, user, access[0].id),
            "station": access[0],
            "role": access[1],
        },
    )


@router.get("/api/v1/stations/{station_id}/solar", response_model=SolarSnapshot)
def snapshot(response: Response, access=Depends(viewer), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return solar_service.snapshot(db, access[0])


@router.get(
    "/api/v1/stations/{station_id}/solar/trackers/{tracker_id}/history", response_model=SolarHistory
)
def history(
    tracker_id: UUID,
    response: Response,
    range: Literal["24h", "7d", "30d"] = "24h",
    access=Depends(viewer),
    db: Session = Depends(get_db),
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return solar_service.history(db, access[0], tracker_id, range)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put(
    "/api/v1/stations/{station_id}/solar/trackers/{tracker_id}", dependencies=[Depends(verify_csrf)]
)
def configure(
    tracker_id: UUID,
    data: TrackerConfiguration,
    access=Depends(operator),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tracker = db.scalar(
        select(SolarTracker)
        .join(SolarInverter)
        .where(SolarInverter.station_id == access[0].id, SolarTracker.id == tracker_id)
        .with_for_update(of=SolarTracker)
    )
    if tracker is None:
        raise HTTPException(404, "Intrare inexistenta.")
    if tracker.revision != data.revision:
        raise HTTPException(409, "Configuratia a fost modificata. Reincarca pagina.")
    tracker.label = data.label
    tracker.configuration = data.model_dump(mode="json", exclude={"revision", "label"})
    tracker.revision += 1
    record_audit(
        db,
        action="solar.tracker_configured",
        resource_type="solar_tracker",
        resource_id=str(tracker.id),
        actor_user_id=user.id,
        station_id=access[0].id,
        organization_id=access[0].organization_id,
        metadata={"revision": tracker.revision, "comparison_enabled": bool(data.comparison_group)},
    )
    db.commit()
    return {"revision": tracker.revision, "physical_write": False}
