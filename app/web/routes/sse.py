from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.deps import StationAccess
from app.database import SessionLocal
from app.services import dashboard_service

router = APIRouter()

POLL_INTERVAL_SECONDS = 5


@router.get("/stations/{station_id}/sse")
async def station_live_stream(
    station_id: uuid.UUID,
    request: Request,
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
):
    """Flux SSE cu valorile live ale statiei. Clientul (app.js) se reconecteaza
    automat cu backoff exponential daca fluxul se intrerupe."""
    station, _role = station_role
    station_id_value = station.id

    async def event_generator():
        last_payload = None
        while True:
            if await request.is_disconnected():
                break
            db: Session = SessionLocal()
            try:
                from app.models.station import Station

                station_obj = db.get(Station, station_id_value)
                if station_obj is None:
                    break
                summary = dashboard_service.get_summary(db, station_obj)
            finally:
                db.close()

            payload = json.dumps(summary, default=str)
            if payload != last_payload:
                yield {"event": "summary", "data": payload}
                last_payload = payload
            else:
                yield {"event": "heartbeat", "data": "{}"}

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return EventSourceResponse(event_generator())
