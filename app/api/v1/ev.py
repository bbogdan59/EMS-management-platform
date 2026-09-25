from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.database import get_db
from app.schemas.energy_operations import EVObservationIn
from app.services.ev_service import ingest_observation

router = APIRouter()


@router.post("/ev/connectors/{connector_id}/observations")
def report_observation(connector_id: uuid.UUID, payload: EVObservationIn,
                       device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    try:
        observation, status = ingest_observation(db, device, connector_id, payload)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return {"event_id": observation.event_id, "status": status, "applied": observation.applied}
