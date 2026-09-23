"""Enrollment automat al dispozitivelor neasociate (issue #16), distinct de
fluxul clasic cu cod de asociere (`app/api/v1/devices.py::claim_device`,
neatins aici). Vezi `app/services/device_service.py` (sectiunea "Enrollment
automat") si docs/API.md pentru contractul complet, cu exemple."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import enforce_payload_limit
from app.config import get_settings
from app.core.audit import record_audit
from app.database import get_db
from app.models.device import Device
from app.schemas.enrollment_api import EnrollRequest, EnrollResponse
from app.services import device_service, firmware_service, firmware_storage

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
            agent_version=payload.agent_version, build_id=payload.build_id,
            hardware_platform=payload.hardware_platform, architecture=payload.architecture,
            os_version=payload.os_version,
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

    # Issue #168: a pending/unlinked device's ONLY authenticated channel is
    # this idempotent enrollment call (no Bearer credential yet) -- surface
    # any active OTA offer here too, not only via the dedicated poll
    # endpoint that active/credentialed devices use.
    if result["device_id"] is not None:
        device = db.get(Device, result["device_id"])
        if device is not None:
            offer = firmware_service.get_current_offer(db, device)
            if offer is not None:
                settings = get_settings()
                storage = firmware_storage.get_release_storage(settings)
                result["firmware_offer"] = firmware_service.offer_payload(db, offer, storage)

    db.commit()
    return EnrollResponse(**result)
