"""Signed keyset pagination; lists use one bounded query, never per-row analytics."""

from datetime import datetime
from uuid import UUID

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import func, select, tuple_

from app.config import get_settings
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.ev import ChargingSession
from app.models.health import AlertEvent
from app.models.notification import Notification
from app.schemas.mobile import (
    MobileChargingSession,
    MobileChargingSessions,
    MobileMetric,
    MobileNotification,
    MobileNotifications,
)
from app.services.health_service import RULES


def serializer():
    return URLSafeTimedSerializer(get_settings().secret_key, salt="mobile-keyset-v1")


def cursor_state(cursor, station, user, collection):
    scope = [str(station.id), str(user.id), collection]
    if not cursor:
        return {"scope": scope, "anchor": utcnow().isoformat()}, None
    try:
        state = serializer().loads(cursor, max_age=86400)
        if state["scope"] != scope:
            raise ValueError("wrong scope")
        return state, (datetime.fromisoformat(state["at"]), UUID(state["id"]))
    except (BadData, ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid_cursor") from exc


def next_cursor(state, rows, limit, field):
    if len(rows) <= limit:
        return None
    row = rows[limit - 1]
    return serializer().dumps({**state, "at": getattr(row, field).isoformat(), "id": str(row.id)})


def notifications(db, station, user, cursor, limit, unread):
    state, after = cursor_state(cursor, station, user, f"notifications:{unread}")
    base = (Notification.station_id == station.id, Notification.user_id == user.id)
    query = (
        select(Notification, AlertEvent.status, Alert.description, Alert.category)
        .outerjoin(AlertEvent, AlertEvent.id == Notification.event_id)
        .outerjoin(Alert, (Alert.id == AlertEvent.alert_id) & (Alert.station_id == station.id))
        .where(*base, Notification.created_at <= datetime.fromisoformat(state["anchor"]))
    )
    if unread:
        query = query.where(Notification.read_at.is_(None))
    if after:
        query = query.where(tuple_(Notification.created_at, Notification.id) < after)
    rows = db.execute(
        query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1)
    ).all()
    items = []
    for notice, status, description, category in rows[:limit]:
        rule = RULES.get(category)
        body = (notice.payload or {}).get("body") or (rule.action if rule else description)
        items.append(
            MobileNotification(
                id=notice.id,
                title=notice.title,
                body=str(body)[:2000] if body else None,
                category=notice.category,
                severity=notice.severity,
                state=status,
                created_at=notice.created_at,
                read_at=notice.read_at,
                link=notice.link
                if notice.link.startswith("/") and not notice.link.startswith("//")
                else None,
            )
        )
    count = db.scalar(
        select(func.count(Notification.id)).where(*base, Notification.read_at.is_(None))
    )
    return MobileNotifications(
        station_id=station.id,
        items=items,
        unread_count=count,
        next_cursor=next_cursor(state, [r[0] for r in rows], limit, "created_at"),
    )


def charging_sessions(db, station, user, cursor, limit):
    state, after = cursor_state(cursor, station, user, "charging_sessions")
    query = select(ChargingSession).where(
        ChargingSession.station_id == station.id,
        ChargingSession.created_at <= datetime.fromisoformat(state["anchor"]),
    )
    if after:
        query = query.where(tuple_(ChargingSession.started_at, ChargingSession.id) < after)
    rows = db.scalars(
        query.order_by(ChargingSession.started_at.desc(), ChargingSession.id.desc()).limit(
            limit + 1
        )
    ).all()
    return MobileChargingSessions(
        station_id=station.id,
        next_cursor=next_cursor(state, rows, limit, "started_at"),
        items=[
            MobileChargingSession(
                id=row.id,
                connector_id=row.connector_id,
                vehicle_id=row.vehicle_id,
                started_at=row.started_at,
                ended_at=row.ended_at,
                state=row.state,
                reason_codes=row.reason_codes,
                energy=MobileMetric(
                    value=row.energy_kwh,
                    unit="kWh",
                    source=row.source,
                    quality=row.quality if row.energy_kwh is not None else "missing",
                    updated_at=row.updated_at,
                ),
            )
            for row in rows[:limit]
        ],
    )
