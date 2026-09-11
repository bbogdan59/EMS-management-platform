from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.database import get_db
from app.schemas.device_api import CommandAckRequest, CommandOut, CommandResultRequest
from app.services import device_service

router = APIRouter()


@router.get("/commands/pending", response_model=list[CommandOut])
def list_pending_commands(device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    commands = device_service.list_pending_commands(db, device)
    db.commit()
    return [
        CommandOut(
            command_id=c.id,
            type=c.type,
            parameters=c.parameters,
            version=c.version,
            idempotency_key=c.idempotency_key,
            reason=c.reason,
            author=c.author,
            valid_from=c.valid_from,
            expires_at=c.expires_at,
        )
        for c in commands
    ]


@router.post("/commands/{command_id}/ack")
def acknowledge_command(
    command_id: uuid.UUID,
    payload: CommandAckRequest,
    device=Depends(get_authenticated_device),
    db: Session = Depends(get_db),
):
    """Platforma NU e mecanismul de protectie electrica: dispozitivul local
    poate respinge orice comanda dupa propria sa validare de siguranta."""
    try:
        command = device_service.acknowledge_command(db, device, command_id, payload.status, payload.reason)
    except device_service.CommandExpiredError as exc:
        # Serviciul marcheaza tranzitia, dar limita tranzactiei ramane in ruta:
        # nu permite unui helper reutilizabil sa comita alte schimbari ale apelantului.
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except device_service.DeviceServiceError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return {"command_id": command.id, "status": command.status}


@router.post("/commands/{command_id}/result")
def report_command_result(
    command_id: uuid.UUID,
    payload: CommandResultRequest,
    device=Depends(get_authenticated_device),
    db: Session = Depends(get_db),
):
    """Starea 'executata' provine EXCLUSIV din acest raport al dispozitivului,
    niciodata din simpla salvare/publicare a comenzii in platforma."""
    try:
        command = device_service.report_command_result(
            db, device, command_id, payload.status, payload.details, payload.error_message
        )
    except device_service.DeviceServiceError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return {"command_id": command.id, "status": command.status}
