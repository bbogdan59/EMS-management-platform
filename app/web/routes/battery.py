from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.database import get_db
from app.schemas.battery import BatteryDetail, BatterySummary
from app.services import battery_service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
viewer = StationAccess("viewer")


@router.get("/stations/{station_id}/battery")
def battery_page(request: Request, access=Depends(viewer), user=Depends(get_current_user), db: Session = Depends(get_db)):
    station, role = access
    return templates.TemplateResponse(request, "battery/detail.html", {
        **build_nav_context(db, user, station.id), "station": station, "role": role,
    })


@router.get("/api/v1/stations/{station_id}/battery-health/summary", response_model=BatterySummary)
def battery_summary(access=Depends(viewer), db: Session = Depends(get_db)):
    """Current observations per device/bank or pack. Human station viewer authorization."""
    return battery_service.summary(db, access[0])


@router.get("/api/v1/stations/{station_id}/battery-health", response_model=BatteryDetail)
def battery_detail(battery_id: str | None = Query(None, max_length=128), days: int = Query(14, ge=1, le=31),
                   end: date | None = None, day: date | None = None,
                   access=Depends(viewer), db: Session = Depends(get_db)):
    """Bounded aggregates in the station calendar; Decimal values serialize as strings.

    Positive DC power charges, negative discharges. Missing values remain null.
    EFC is observed throughput / (2 * historical nominal capacity), not lifetime SOH.
    `today` describes the selected `day`. Quality/coverage accompany every series.
    """
    try:
        return battery_service.detail(db, access[0], battery_id=battery_id, days=days, end=end, day=day)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
