"""Outbound-only HACS API; cookie management and bearer ingestion stay separate."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.database import get_db
from app.models.station import Station
from app.schemas.home_assistant_bridge import Ingest, MappingUpdate, PairingRedeem
from app.services import home_assistant_bridge as service

router = APIRouter()
edit_access = StationAccess(min_role="organization_admin")
view_access = StationAccess()


def response(data, status=200):
    return JSONResponse(
        jsonable_encoder(data), status_code=status, headers={"Cache-Control": "no-store"}
    )


def enabled():
    if not get_settings().home_assistant_bridge_enabled:
        raise HTTPException(503, "bridge_disabled")


def limit(key, count, seconds):
    try:
        check_fixed_window(key, count, seconds)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429, "rate_limited", headers={"Retry-After": str(exc.retry_after_seconds)}
        ) from None


async def body(request, model):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise HTTPException(413, "payload_too_large")
    try:
        return model.model_validate_json(bytes(raw))
    except ValidationError:
        # Never reflect pairing codes, bearer credentials or raw sensor values.
        raise HTTPException(422, "invalid_payload") from None


def authenticated(request: Request, db: Session = Depends(get_db)):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "reauth_required")
    token = authorization.removeprefix("Bearer ")
    bridge = service.authenticate(db, token)
    if bridge is None:
        raise HTTPException(401, "reauth_required")
    limit(f"ha_bridge:{bridge.id}", 60, 60)
    return bridge


@router.post(
    "/stations/{station_id}/home-assistant/pairing",
    dependencies=[Depends(verify_csrf), Depends(enabled)],
)
def create_pairing(
    db: Session = Depends(get_db), access=Depends(edit_access), user=Depends(get_current_user)
):
    station, _ = access
    limit(f"ha_pairing_issue:{station.id}", 10, 3600)
    bridge, code = service.issue_pairing(db, station, user)
    db.commit()
    return response(
        {"schema_version": 1, "code": code, "expires_at": bridge.pairing_expires_at}, 201
    )


@router.get("/stations/{station_id}/home-assistant")
def status(db: Session = Depends(get_db), access=Depends(view_access)):
    bridge = service.get_bridge(db, access[0].id)
    active = get_settings().home_assistant_bridge_enabled and (
        bridge is None or service.owner_active(db, bridge)
    )
    return response(service.snapshot(bridge, enabled=active))


@router.delete("/stations/{station_id}/home-assistant", dependencies=[Depends(verify_csrf)])
def disconnect(
    db: Session = Depends(get_db), access=Depends(edit_access), user=Depends(get_current_user)
):
    bridge = service.get_bridge(db, access[0].id, lock=True)
    if bridge:
        service.revoke(bridge)
        service.audit(db, access[0], user, "revoked", bridge)
        db.commit()
    return response({"status": "disconnected"})


@router.post("/home-assistant/pairing/redeem", dependencies=[Depends(enabled)])
async def redeem(request: Request, db: Session = Depends(get_db)):
    limit(f"ha_pairing_redeem:{request.client.host if request.client else 'unknown'}", 10, 60)
    data = await body(request, PairingRedeem)
    try:
        bridge, token = service.redeem(db, data)
    except ValueError:
        raise HTTPException(400, "invalid_pairing") from None
    station = db.get(Station, bridge.station_id)
    db.commit()
    return response(
        {
            "schema_version": 1,
            "token": token,
            "token_expires_at": bridge.token_expires_at,
            "station_id": station.id,
            "station_name": station.name,
            "instance_id": bridge.instance_id,
            "mapping_version": bridge.mapping_version,
        }
    )


@router.put("/home-assistant/bridge/mappings", dependencies=[Depends(enabled)])
async def mappings(request: Request, db: Session = Depends(get_db), bridge=Depends(authenticated)):
    data = await body(request, MappingUpdate)
    try:
        service.configure(db, bridge, data)
    except ValueError:
        raise HTTPException(409, "mapping_conflict") from None
    db.commit()
    return response({"schema_version": 1, "mapping_version": bridge.mapping_version})


@router.post("/home-assistant/bridge/samples", dependencies=[Depends(enabled)])
async def samples(request: Request, db: Session = Depends(get_db), bridge=Depends(authenticated)):
    data = await body(request, Ingest)
    try:
        result = service.ingest(bridge, data)
    except ValueError as exc:
        code = "mapping_conflict" if str(exc) == "mapping_conflict" else "invalid_sample"
        raise HTTPException(409 if code == "mapping_conflict" else 422, code) from None
    db.commit()
    return response({"schema_version": 1, **result})


@router.delete("/home-assistant/bridge")
def remove(db: Session = Depends(get_db), bridge=Depends(authenticated)):
    service.audit_bridge(db, bridge, "self_revoked")
    service.revoke(bridge)
    db.commit()
    return response({"status": "disconnected"})
