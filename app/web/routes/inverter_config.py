import json
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user, require_platform_admin
from app.api.v1.inverter_config import envelope
from app.core.csrf import verify_csrf
from app.core.security import utcnow
from app.database import get_db
from app.models.device import Device
from app.models.firmware import FirmwareDeployment
from app.models.inverter_config import InverterDesired, InverterProfile
from app.schemas.inverter_config import DesiredRequest, Profile
from app.services import device_service
from app.services import inverter_config_service as service
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()
view_access = StationAccess()
edit_access = StationAccess(min_role="organization_admin")


def scoped_device(db, station, device_id):
    device = db.get(Device, device_id)
    if device is None or device.station_id != station.id:
        raise HTTPException(404, 'Device not found in this station')
    return device


@router.post('/admin/inverter-profiles', dependencies=[Depends(verify_csrf)])
def approve_profile(payload: Profile, user=Depends(require_platform_admin), db: Session = Depends(get_db)):
    row = service.approved_profile(db, payload, user)
    db.commit()
    return {'profile_id': row.id, 'hash': row.digest}


@router.get('/stations/{station_id}/devices/{device_id}/configuration')
def page(request: Request, device_id: uuid.UUID, db: Session = Depends(get_db),
         access=Depends(view_access), user=Depends(get_current_user)):
    station, role = access
    device = scoped_device(db, station, device_id)
    state = envelope(db, device.id)
    profiles = db.scalars(select(InverterProfile).order_by(InverterProfile.created_at)).all()
    history = db.scalars(select(InverterDesired).where(InverterDesired.device_id == device.id)
                         .order_by(InverterDesired.version.desc()).limit(20)).all()
    device_logs = device_service.list_recent_device_logs(db, device)
    firmware_deployments = db.scalars(
        select(FirmwareDeployment).where(FirmwareDeployment.device_id == device.id)
        .order_by(FirmwareDeployment.requested_at.desc()).limit(10)
    ).all()
    return templates.TemplateResponse(request, 'stations/inverter_config.html', {
        'station': station, 'device': device, 'state': state, 'profiles': profiles, 'history': history,
        'device_logs': device_logs, 'log_retention_days': device_service.DEVICE_LOG_RETENTION.days,
        'firmware_deployments': firmware_deployments,
        'now': utcnow(),
        'can_edit': role in ('organization_admin', 'platform_admin'), 'request_id': str(uuid.uuid4()),
        **build_nav_context(db, user, station.id),
    })


@router.post('/stations/{station_id}/devices/{device_id}/configuration', dependencies=[Depends(verify_csrf)])
def save(device_id: uuid.UUID, payload: DesiredRequest, db: Session = Depends(get_db),
         access=Depends(edit_access), user=Depends(get_current_user)):
    station, _role = access
    scoped_device(db, station, device_id)
    try:
        row = service.save_desired(db, station, device_id, payload, user)
        db.commit()
    except service.ConfigConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {'version': row.version, 'hash': row.digest, 'command_id': row.command_id}


@router.post('/stations/{station_id}/devices/{device_id}/configuration/form', dependencies=[Depends(verify_csrf)])
def save_form(device_id: uuid.UUID, request_id: str = Form(...), expected_version: int = Form(...),
              profile_id: str = Form(...), port: str = Form(...), slave: int = Form(...),
              baudrate: int = Form(...), parity: str = Form(...), stopbits: int = Form(...),
              sample_seconds: int = Form(...), settings_json: str = Form('{}'), reason: str = Form(...),
              db: Session = Depends(get_db), access=Depends(edit_access),
              user=Depends(get_current_user)):
    try:
        payload = DesiredRequest.model_validate({
            'request_id': request_id, 'expected_version': expected_version, 'profile_id': profile_id,
            'connection': {'port': port, 'device_id': slave, 'baudrate': baudrate, 'parity': parity,
                           'stopbits': stopbits, 'sample_seconds': sample_seconds},
            'settings': json.loads(settings_json), 'reason': reason,
        })
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, 'Invalid configuration; check values and approved profile limits.') from exc
    save(device_id=device_id, payload=payload, db=db, access=access, user=user)
    return RedirectResponse(f'/stations/{access[0].id}/devices/{device_id}/configuration', status_code=303)
