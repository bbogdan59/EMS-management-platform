"""Enrollment automat al dispozitivelor neasociate (issue #16), distinct de
fluxul clasic cu cod de asociere (`app/api/v1/devices.py::claim_device`,
neatins aici). Vezi `app/services/device_service.py` (sectiunea "Enrollment
automat") si docs/API.md pentru contractul complet, cu exemple."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import enforce_payload_limit
from app.core.audit import record_audit
from app.database import get_db
from app.schemas.enrollment_api import EnrollRequest, EnrollResponse
from app.services import device_service

router = APIRouter()


@router.post(
    "/devices/enroll",
    response_model=EnrollResponse,
    dependencies=[Depends(enforce_payload_limit)],
)
def enroll_device(payload: EnrollRequest, db: Session = Depends(get_db)):
    """Idempotent: se poate retrimite oricand cu aceeasi identitate
    (installation_uuid + provisioning_secret) -- vezi docstring-ul
    `device_service.enroll_device`. NU necesita autentificare Bearer
    (dispozitivul nu are inca nicio credentiala emisa de server); dovada de
    identitate e secretul de provisioning propriu, transmis in corp."""
    try:
        result = device_service.enroll_device(
            db, payload.installation_uuid, payload.provisioning_secret, payload.hardware_info,
            serial_number=payload.serial_number, activation_code=payload.activation_code,
        )
    except device_service.DeviceServiceError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    record_audit(
        db, action="device_enrolled", resource_type="device",
        resource_id=str(result["device_id"]) if result["device_id"] else None,
        actor_label=f"installation:{payload.installation_uuid}",
        metadata={"status": result["status"], "installation_uuid": payload.installation_uuid},
    )
    db.commit()
    return EnrollResponse(**result)
