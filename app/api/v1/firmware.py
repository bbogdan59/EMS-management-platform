"""Device-facing OTA firmware API (issue #168) -- for an already-assigned,
credentialed device. A pending/unlinked device instead receives an offer
through `POST /devices/enroll` (see app/api/v1/enrollment.py), since it has
no Bearer credential yet."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.config import get_settings
from app.database import get_db
from app.schemas.device_api import FirmwareDeploymentEventRequest, FirmwareOfferOut
from app.services import firmware_service, firmware_storage

router = APIRouter()


@router.get("/firmware/pending", response_model=FirmwareOfferOut | None)
def get_pending_firmware_offer(device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    offer = firmware_service.get_current_offer(db, device)
    if offer is None:
        db.commit()
        return None
    settings = get_settings()
    storage = firmware_storage.get_release_storage(settings)
    payload = firmware_service.offer_payload(db, offer, storage)
    db.commit()
    return FirmwareOfferOut(**payload)


@router.post("/firmware/deployments/{deployment_id}/events")
def report_firmware_deployment_event(
    deployment_id: uuid.UUID,
    payload: FirmwareDeploymentEventRequest,
    device=Depends(get_authenticated_device),
    db: Session = Depends(get_db),
):
    """The device's own progress report drives the state machine -- the
    platform never marks `succeeded` on its own initiative, only from a
    `confirmed` event carrying the exact target version and a genuinely new
    boot_id (see firmware_service._resolve_confirmation)."""
    try:
        deployment = firmware_service.report_deployment_event(
            db, device, deployment_id, payload.event_type, payload=payload.payload, message=payload.message,
        )
    except firmware_service.FirmwareServiceError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return {"deployment_id": deployment.id, "status": deployment.status}
