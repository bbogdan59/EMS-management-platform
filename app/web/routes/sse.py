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
from app.core.security import utcnow
from app.database import SessionLocal
from app.models.organization import Membership
from app.models.station import Station
from app.models.user import Session as UserSession
from app.models.user import User
from app.services import dashboard_service

router = APIRouter()

POLL_INTERVAL_SECONDS = 5


def _authorize_station_access(db: Session, station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID) -> Station | None:
    """Re-verificata la FIECARE ciclu de polling (nu doar la deschiderea
    conexiunii) -- vezi `tests/unit/test_sse_authorization.py`. O sesiune
    revocata sau o membership dezactivata opreste fluxul in cel mult
    `POLL_INTERVAL_SECONDS`."""
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
    return station


def _authorized_summary(db: Session, station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID):
    station = _authorize_station_access(db, station_id, user_id, session_id)
    return dashboard_service.get_summary(db, station) if station is not None else None


def _authorized_live_metrics(db: Session, station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID) -> list[dict] | None:
    station = _authorize_station_access(db, station_id, user_id, session_id)
    return dashboard_service.get_live_metrics(db, station) if station is not None else None


def _load_authorized_live_metrics(station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID) -> list[dict] | None:
    with SessionLocal() as db:
        return _authorized_live_metrics(db, station_id, user_id, session_id)


def diff_metrics(previous_by_metric: dict[str, dict], current: list[dict]) -> list[dict]:
    """Metricile din `current` a caror `value`/`quality` difera fata de
    ultima emisie (`previous_by_metric`, cheie = nume metrica). Motorul de
    coalescing al fluxului -- doar schimbarile ajung intr-un eveniment
    `delta`, indiferent cate ori s-a schimbat fizic valoarea intre doua
    tururi de polling (vezi ADR 0001)."""
    changed = []
    for entry in current:
        previous = previous_by_metric.get(entry["metric"])
        if previous is None or previous["value"] != entry["value"] or previous["quality"] != entry["quality"]:
            changed.append(entry)
    return changed


async def live_metric_events(request: Request, station_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID):
    """Generatorul propriu-zis al fluxului, extras din ruta ca sa poata fi
    testat direct (fara transport ASGI real) -- vezi
    `tests/unit/test_sse_live_stream.py`. Prima emisie a FIECAREI conexiuni
    (initiala sau reconectare) e intotdeauna un `snapshot` complet -- serverul
    nu pastreaza un jurnal de evenimente pentru replay partial, deci nu
    promite niciodata o continuitate de secventa pe care nu o poate garanta
    (vezi ADR 0001)."""
    sequence = 0
    last_by_metric: dict[str, dict] = {}
    is_first = True
    while True:
        if await request.is_disconnected():
            break
        metrics = await asyncio.to_thread(
            _load_authorized_live_metrics,
            station_id,
            user_id,
            session_id,
        )
        if metrics is None:
            break

        sequence += 1
        generated_at = utcnow().isoformat()

        if is_first:
            payload = json.dumps({"sequence": sequence, "generated_at": generated_at, "metrics": metrics}, default=str)
            yield {"event": "snapshot", "id": str(sequence), "data": payload}
            last_by_metric = {m["metric"]: m for m in metrics}
            is_first = False
        else:
            changed = diff_metrics(last_by_metric, metrics)
            if changed:
                payload = json.dumps({"sequence": sequence, "generated_at": generated_at, "metrics": changed}, default=str)
                yield {"event": "delta", "id": str(sequence), "data": payload}
                for entry in changed:
                    last_by_metric[entry["metric"]] = entry
            else:
                yield {"event": "heartbeat", "id": str(sequence), "data": json.dumps({"sequence": sequence, "generated_at": generated_at})}

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@router.get("/stations/{station_id}/sse")
async def station_live_stream(
    station_id: uuid.UUID,
    request: Request,
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    auth_context: AuthContext = Depends(get_current_context),
):
    """Flux SSE cu valorile live ale statiei (issue #6, extins la un contract
    per-metrica versionat de issue #50 -- vezi `docs/adr/0001-realtime-dashboard-transport.md`).
    Clientul (app.js) se reconecteaza automat cu backoff exponential daca
    fluxul se intrerupe."""
    station, _role = station_role
    return EventSourceResponse(
        live_metric_events(request, station.id, auth_context.user.id, auth_context.session.id)
    )
