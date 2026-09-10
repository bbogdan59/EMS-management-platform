from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.core.audit import record_audit
from app.database import get_db
from app.schemas.device_api import (
    ClaimRequest,
    ClaimResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    RotateCredentialResponse,
)
from app.services import device_service
from app.core.security import utcnow
from app.models.command import Command
from app.models.enums import CommandStatus, PlanStatus
from app.models.optimization import Plan
from sqlalchemy import select

router = APIRouter()


@router.post("/devices/claim", response_model=ClaimResponse, status_code=status.HTTP_201_CREATED)
def claim_device(payload: ClaimRequest, db: Session = Depends(get_db)):
    """Asociaza un dispozitiv nou folosind un cod de asociere cu expirare,
    generat in prealabil de un operator/admin in UI. Secretul returnat este
    afisat o singura data -- dispozitivul trebuie sa il stocheze local."""
    try:
        device, secret = device_service.claim_device(db, payload.claim_code, payload.device_name, payload.hardware_info)
    except device_service.DeviceServiceError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))

    record_audit(
        db, action="device_claimed", resource_type="device", resource_id=str(device.id),
        actor_label=f"device:{device.id}", station_id=device.station_id,
    )
    db.commit()
    return ClaimResponse(device_id=device.id, station_id=device.station_id, credential_secret=secret)


@router.post("/devices/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    payload: HeartbeatRequest,
    device=Depends(get_authenticated_device),
    db: Session = Depends(get_db),
):
    device = device_service.record_heartbeat(db, device, payload.boot_id, payload.firmware_version, payload.capabilities)

    has_active_plan = (
        db.scalar(
            select(Plan.id).where(
                Plan.station_id == device.station_id,
                Plan.status.in_(
                    [PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value]
                ),
            )
        )
        is not None
    )
    from sqlalchemy import func

    pending_command_count = db.scalar(
        select(func.count(Command.id)).where(
            Command.device_id == device.id,
            Command.status.in_([CommandStatus.created.value, CommandStatus.delivered.value]),
            Command.expires_at >= utcnow(),
        )
    ) or 0

    db.commit()
    return HeartbeatResponse(
        server_time=utcnow(),
        device_status=device.status,
        has_active_plan=has_active_plan,
        pending_command_count=pending_command_count,
    )


@router.post("/devices/credentials/rotate", response_model=RotateCredentialResponse)
def rotate_credential(device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    """Rotatie de credentiale: cere autentificare cu credentiala CURENTA.
    Vechea credentiala e revocata imediat -- dispozitivul trebuie sa foloseasca
    noul secret din acest raspuns la urmatoarea cerere."""
    secret = device_service.rotate_credential(db, device)
    record_audit(
        db, action="device_credential_rotated", resource_type="device", resource_id=str(device.id),
        actor_label=f"device:{device.id}", station_id=device.station_id,
    )
    db.commit()
    return RotateCredentialResponse(device_id=device.id, credential_secret=secret)
