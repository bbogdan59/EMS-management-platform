from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.api.v1.device_deps import get_authenticated_device
from app.database import get_db
from app.models.command import Command
from app.schemas.inverter_config import ReportRequest
from app.services import inverter_config_service as service

router = APIRouter()


def envelope(db, device_id):
    desired, reported, status = service.reconciliation(db, device_id)
    command = db.get(Command, desired.command_id) if desired and desired.command_id else None
    profile = service.checked_profile(db, desired.profile_id)[0] if desired else None
    return jsonable_encoder({
        'schema_version': 1, 'status': status,
        'command': None if command is None else {'status': command.status, 'expires_at': command.expires_at, 'error': command.last_error},
        'desired': None if desired is None else {
            'version': desired.version, 'hash': desired.digest, 'profile_id': desired.profile_id,
            'profile_hash': profile.digest, 'profile': profile.definition,
            'connection': desired.connection, 'settings': desired.settings,
            'reason': desired.reason, 'created_by': desired.created_by, 'created_at': desired.created_at,
            'command_id': desired.command_id,
        },
        'reported': None if reported is None else {
            'report_id': reported.id, 'version': reported.version, 'desired_version': reported.desired_version,
            'hash': reported.digest, 'settings': reported.snapshot,
            'model': reported.model, 'firmware': reported.firmware,
            'measured_at': reported.measured_at, 'outcome': reported.outcome, 'reason': reported.reason,
        },
    })


@router.get('/inverter/config')
def config(device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    return envelope(db, device.id)


@router.post('/inverter/reported')
def reported(payload: ReportRequest, device=Depends(get_authenticated_device), db: Session = Depends(get_db)):
    try:
        row = service.report_snapshot(db, device.id, payload)
        db.commit()
    except service.ConfigConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {'report_id': row.id, 'version': row.version, 'hash': row.digest, 'request_id': row.request_id}
