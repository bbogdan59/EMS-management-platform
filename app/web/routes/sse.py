from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.deps import AuthContext, StationAccess, get_current_context
from app.core.rbac import role_at_least
from app.database import SessionLocal
from app.models.organization import Membership
from app.models.station import Station
from app.models.user import Session as UserSession
from app.models.user import User
from app.services import dashboard_service

router = APIRouter()

POLL_INTERVAL_SECONDS = 5


def _authorized_summary(db: Session, station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID):
    session = db.get(UserSession, session_id)
    user = db.get(User, user_id)
    station = db.get(Station, station_id)
    if session is None or session.user_id != user_id or not session.is_valid:
        return None
    if user is None or not user.is_active or station is None:
        return None
    if not user.is_platform_admin:
        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.organization_id == station.organization_id,
                Membership.is_active.is_(True),
            )
        )
        if membership is None or not role_at_least(membership.role, "viewer"):
            return None
    return dashboard_service.get_summary(db, station)


def _load_authorized_summary(station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID):
    with SessionLocal() as db:
        return _authorized_summary(db, station_id, user_id, session_id)


@router.get("/stations/{station_id}/sse")
async def station_live_stream(
    station_id: uuid.UUID,
    request: Request,
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    auth_context: AuthContext = Depends(get_current_context),
):
    """Flux SSE cu valorile live ale statiei. Clientul (app.js) se reconecteaza
    automat cu backoff exponential daca fluxul se intrerupe."""
    station, _role = station_role
    station_id_value = station.id
    user_id = auth_context.user.id
    session_id = auth_context.session.id

    async def event_generator():
        last_payload = None
        while True:
            if await request.is_disconnected():
                break
            summary = await asyncio.to_thread(
                _load_authorized_summary,
                station_id_value,
                user_id,
                session_id,
            )
            if summary is None:
                break

            payload = json.dumps(summary, default=str)
            if payload != last_payload:
                yield {"event": "summary", "data": payload}
                last_payload = payload
            else:
                yield {"event": "heartbeat", "data": "{}"}

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return EventSourceResponse(event_generator())
