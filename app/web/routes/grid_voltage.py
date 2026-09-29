from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import StationAccess
from app.database import get_db
from app.schemas.grid_voltage import GridVoltageHistory
from app.services import grid_voltage_service

router = APIRouter()
viewer = StationAccess("viewer")


@router.get("/api/v1/stations/{station_id}/grid-voltage", response_model=GridVoltageHistory)
def history(
    response: Response,
    day: date | None = None,
    source_id: str | None = Query(None, max_length=100),
    access=Depends(viewer),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return grid_voltage_service.history(db, access[0], day, source_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
