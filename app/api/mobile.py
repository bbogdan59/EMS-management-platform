from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.mobile_support import MobileRoute, access, fail, private_response
from app.database import get_db
from app.schemas.mobile import (
    MobileChargingSessions,
    MobileChart,
    MobileError,
    MobileHAContext,
    MobileNotifications,
    MobileOverview,
)
from app.services import mobile_pagination as pages
from app.services import mobile_service as service

router = APIRouter(
    prefix="/api/v1/mobile",
    route_class=MobileRoute,
    tags=["mobile-v1"],
    responses={code: {"model": MobileError} for code in (401, 403, 404, 409, 422, 429, 503)},
)


@router.get(
    "/overview",
    response_model=MobileOverview,
    responses={304: {"description": "Private representation unchanged after reauthorization"}},
)
def overview(request: Request, scope=Depends(access), db: Session = Depends(get_db)):
    """Single first-screen read. Optional station_id auto-selects only a sole accessible station."""
    station, role, user = scope
    result = service.overview(db, station, user, role)
    return private_response(request, result, modified=result.evaluated_at, conditional=True)


@router.get(
    "/charts/energy",
    response_model=MobileChart,
    responses={304: {"description": "Private chart unchanged after reauthorization"}},
)
def chart(
    request: Request,
    range: Literal["24h", "7d", "30d", "1y"] = "24h",
    resolution: Literal["15m", "1h", "1d"] | None = None,
    end: datetime | None = None,
    scope=Depends(access),
    db: Session = Depends(get_db),
):
    """Energy sums and mean SOC from retained rollups; maximum 768 buckets, no raw samples."""
    if end and (end.tzinfo is None or end > datetime.now(UTC) or end.year < 1971):
        fail(
            422,
            "invalid_request",
            "Finalul trebuie sa fie un instant cu fus orar, intre 1971 si prezent.",
        )
    try:
        result = service.chart(db, scope[0], range, resolution, end=end)
    except ValueError as exc:
        fail(422, "invalid_request", str(exc))
    modified = max((p.updated_at for p in result.points if p.updated_at), default=None)
    return private_response(request, result, modified=modified, conditional=True)


@router.get("/notifications", response_model=MobileNotifications)
def notifications(
    request: Request,
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(20, ge=1, le=50),
    unread: bool = False,
    scope=Depends(access),
    db: Session = Depends(get_db),
):
    try:
        result = pages.notifications(db, scope[0], scope[2], cursor, limit, unread)
    except ValueError:
        fail(422, "invalid_cursor", "Cursor invalid sau expirat.")
    return private_response(request, result)


@router.get("/charging-sessions", response_model=MobileChargingSessions)
def charging_sessions(
    request: Request,
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(20, ge=1, le=50),
    scope=Depends(access),
    db: Session = Depends(get_db),
):
    try:
        result = pages.charging_sessions(db, scope[0], scope[2], cursor, limit)
    except ValueError:
        fail(422, "invalid_cursor", "Cursor invalid sau expirat.")
    return private_response(request, result)


@router.get("/home-assistant/context", response_model=MobileHAContext)
def context(
    request: Request,
    provider: Literal["home_assistant_bridge", "home_assistant_mqtt"] = "home_assistant_bridge",
    scope=Depends(access),
    db: Session = Depends(get_db),
):
    result = service.ha_context(db, scope[0], provider, service.evaluation_time())
    return private_response(request, result)
