"""Station-local day cards, kept in the canonical in-app notification inbox."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert

from app.core.security import utcnow
from app.models.alert import Alert
from app.models.health import AlertEvent
from app.models.notification import Notification
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.telemetry import TelemetryAggregate
from app.models.user import User
from app.services.health_service import RULES

METRICS = (
    ("pv_energy_kwh", "pv", "Productie"),
    ("load_energy_kwh", "load", "Consum"),
    ("grid_import_energy_kwh", "grid", "Import din retea"),
    ("grid_export_energy_kwh", "grid", "Export in retea"),
)


def day_bounds(station, day):
    tz = ZoneInfo(station.timezone)
    return tuple(
        datetime.combine(d, time.min, tzinfo=tz).astimezone(UTC)
        for d in (day, day + timedelta(days=1))
    )


def _complete(row, metric):
    return (
        row is not None
        and row.data_quality == "measured"
        and Decimal(str(row.coverage.get(metric, 0))) >= 1
    )


def day_card(db, station, day):
    start, end = day_bounds(station, day)
    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "day",
            TelemetryAggregate.period_start == start,
            TelemetryAggregate.period_end == end,
        )
    )
    metrics = []
    for field, key, label in METRICS:
        value = getattr(row, field) if row else None
        coverage = Decimal(str(row.coverage.get(key, 0))) if row else Decimal(0)
        metrics.append(
            {
                "label": label,
                "value": str(value) if value is not None else None,
                "coverage": str(coverage),
                "unit": "kWh",
            }
        )
    complete = all(
        _complete(row, key) and getattr(row, field) is not None for field, key, _ in METRICS
    )
    quality = row.data_quality if row else "missing"
    highlights = []
    if _complete(row, "pv") and row.pv_energy_kwh is not None and row.pv_energy_kwh > 0:
        previous = db.scalars(
            select(TelemetryAggregate).where(
                TelemetryAggregate.station_id == station.id,
                TelemetryAggregate.period_type == "day",
                TelemetryAggregate.period_end <= start,
                TelemetryAggregate.data_quality == "measured",
                TelemetryAggregate.pv_energy_kwh.is_not(None),
            )
        ).all()
        comparable = [r for r in previous if _complete(r, "pv")]
        if comparable and row.pv_energy_kwh > max(r.pv_energy_kwh for r in comparable):
            highlights.append(
                {
                    "kind": "record",
                    "title": "Record de productie",
                    "body": f"Cea mai mare productie dintre cele {len(comparable) + 1} zile cu masuratori complete din istoricul disponibil.",
                }
            )
    if complete and row.load_energy_kwh > 0 and row.grid_import_energy_kwh == 0:
        highlights.append(
            {
                "kind": "independence",
                "title": "O zi fara import din retea",
                "body": "Consumul a fost acoperit fara energie importata din retea in aceasta zi.",
            }
        )
    body = "Productia, consumul si schimbul cu reteaua pentru ziua incheiata."
    if quality == "missing":
        body = "Nu sunt inca disponibile masuratori pentru aceasta zi."
    elif quality != "measured":
        body = "Rezumatul include date simulate, intarziate sau derivate. Valorile nu reprezinta exclusiv masuratori reale."
    elif not complete:
        body = "Date incomplete: valorile acopera doar intervalele masurate, nu intreaga zi."
    return {
        "kind": "summary",
        "day": day.isoformat(),
        "timezone": station.timezone,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "body": body,
        "quality": quality,
        "complete": complete,
        "metrics": metrics,
        "highlights": highlights,
        "source_updated_at": row.updated_at.isoformat() if row else None,
    }


def materialize_days(db, now=None):
    now = now or utcnow()
    count = 0
    stations = db.scalars(
        select(Station)
        .join(Organization)
        .where(
            Station.is_active.is_(True),
            Organization.status != "archived",
        )
        .order_by(Station.id)
        .with_for_update(of=Station, key_share=True)
    ).all()
    for station in stations:
        local = now.astimezone(ZoneInfo(station.timezone))
        # Allow the last quarter-hour aggregation to finish before the first card.
        if local.hour == 0 and local.minute < 20:
            continue
        day = local.date() - timedelta(days=1)
        _, end = day_bounds(station, day)
        if station.created_at >= end:
            continue
        payload = day_card(db, station, day)
        users = db.scalars(
            select(Membership.user_id)
            .join(User)
            .where(
                Membership.organization_id == station.organization_id,
                Membership.is_active.is_(True),
                User.is_active.is_(True),
            )
        ).all()
        for user_id in users:
            values = {
                "user_id": user_id,
                "station_id": station.id,
                "source_key": f"day:{day.isoformat()}",
                "payload": payload,
                "title": "Rezumatul zilei",
                "severity": "info",
                "category": "info",
                "link": f"/?station_id={station.id}",
                "routed_at": now,
            }
            # A late aggregate refreshes the same card; read state is retained.
            statement = insert(Notification).values(**values)
            changed_id = db.execute(
                statement.on_conflict_do_update(
                    constraint="uq_notification_source_user",
                    set_={"payload": payload, "updated_at": now},
                    where=Notification.payload.cast(JSONB)
                    != statement.excluded.payload.cast(JSONB),
                ).returning(Notification.id)
            ).scalar_one_or_none()
            count += changed_id is not None
    db.flush()
    return count


def inbox(db, station, user, kind="all", unread=False, offset=0):
    today = utcnow().astimezone(ZoneInfo(station.timezone)).date()
    base = select(Notification).where(
        Notification.station_id == station.id, Notification.user_id == user.id
    )
    unread_count = db.scalar(
        select(func.count()).select_from(base.where(Notification.read_at.is_(None)).subquery())
    )
    if kind == "summary":
        base = base.where(Notification.source_key.is_not(None))
    elif kind == "alert":
        base = base.where(Notification.event_id.is_not(None))
    if unread:
        base = base.where(Notification.read_at.is_(None))
    notices = db.scalars(
        base.order_by(
            case((Notification.source_key == f"day:{today - timedelta(days=1)}", 0), else_=1),
            Notification.created_at.desc(),
            Notification.id.desc(),
        )
        .offset(offset)
        .limit(31)
    ).all()
    items = []
    for notice in notices[:30]:
        payload = dict(notice.payload)
        if notice.event_id:
            event = db.get(AlertEvent, notice.event_id)
            alert = db.get(Alert, event.alert_id)
            rule = RULES.get(alert.category)
            payload = {
                **payload,
                "kind": "alert",
                "state": event.status,
                "body": payload.get("body") or (rule.action if rule else alert.description),
                "occurred_at": event.occurred_at.isoformat(),
            }
        items.append(
            {
                "id": str(notice.id),
                "title": notice.title,
                "severity": notice.severity,
                "created_at": notice.created_at.isoformat(),
                "read": notice.read_at is not None,
                "link": notice.link,
                "payload": payload,
            }
        )
    return {
        "items": items,
        "unread_count": unread_count,
        "has_more": len(notices) > 30,
        "timezone": station.timezone,
        "today": today.isoformat(),
    }
