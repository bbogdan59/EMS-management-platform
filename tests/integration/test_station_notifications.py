from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.notification import Notification, NotificationDelivery
from app.models.organization import Membership, Organization
from app.models.telemetry import TelemetryAggregate
from app.models.user import User
from app.services import health_service as health
from app.services import notification_service as notifications
from app.services import station_notification_service as service
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.integration.test_health_diagnostics import sample
from tests.web_helpers import login


@pytest.fixture()
def context(db):
    user = make_user(db)
    org = make_org(db)
    make_membership(db, user, org, "organization_admin")
    station = make_station(db, org, user)
    station.created_at = utcnow() - timedelta(days=1000)
    device = make_device(db, station)
    at = utcnow().replace(second=0, microsecond=0)
    at = at.replace(minute=at.minute // 5 * 5)
    device.last_heartbeat_at = at
    db.flush()
    return user, org, station, device, at


def aggregate(db, station, day, **changes):
    start, end = service.day_bounds(station, day)
    values = {
        "station_id": station.id,
        "period_type": "day",
        "period_start": start,
        "period_end": end,
        "pv_energy_kwh": Decimal("21.1"),
        "load_energy_kwh": Decimal("13.7"),
        "grid_import_energy_kwh": Decimal(0),
        "grid_export_energy_kwh": Decimal("7.4"),
        "data_quality": "measured",
        "coverage": {"pv": 1, "load": 1, "grid": 1},
    }
    row = TelemetryAggregate(**{**values, **changes})
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2024, 2, 29), 24)]
)
def test_daily_card_uses_station_calendar_and_actual_day_length(db, context, day, hours):
    user, org, station, device, at = context
    station.created_at = service.day_bounds(station, day)[0] - timedelta(days=1)
    aggregate(db, station, day)
    start, end = service.day_bounds(station, day)
    assert (end - start).total_seconds() == hours * 3600
    now = end + timedelta(hours=1)
    service.materialize_days(db, now)
    notice = db.scalar(select(Notification).where(Notification.station_id == station.id))
    assert notice.source_key == f"day:{day}"
    assert notice.payload["start"] == start.isoformat()
    assert notice.payload["end"] == end.isoformat()
    assert notice.payload["complete"]
    assert notice.payload["metrics"][2]["value"] == "0.0000"


@pytest.mark.parametrize(
    "quality,coverage", [("measured", 0.5), ("simulated", 1), ("stale", 1), ("derived", 1)]
)
def test_partial_and_untrusted_days_never_claim_records_or_independence(
    db, context, quality, coverage
):
    user, org, station, device, at = context
    day = at.astimezone(ZoneInfo(station.timezone)).date() - timedelta(days=1)
    aggregate(db, station, day - timedelta(days=1), pv_energy_kwh=Decimal(1))
    aggregate(
        db,
        station,
        day,
        data_quality=quality,
        coverage={"pv": coverage, "grid": 1},
        load_energy_kwh=None,
    )
    payload = service.day_card(db, station, day)
    assert not payload["complete"] and payload["highlights"] == []
    assert payload["quality"] == quality
    assert payload["metrics"][1]["value"] is None
    assert payload["metrics"][1]["coverage"] == "0"
    assert Decimal(payload["metrics"][2]["value"]) == 0


def test_record_compares_measured_complete_days_and_refresh_retains_read_state(db, context):
    user, org, station, device, at = context
    day = at.astimezone(ZoneInfo(station.timezone)).date() - timedelta(days=1)
    _, end = service.day_bounds(station, day)
    aggregate(db, station, day - timedelta(days=3), pv_energy_kwh=Decimal(20))
    aggregate(db, station, day - timedelta(days=2), pv_energy_kwh=Decimal(90), coverage={"pv": 0.5})
    aggregate(
        db, station, day - timedelta(days=1), pv_energy_kwh=Decimal(100), data_quality="simulated"
    )
    current = aggregate(db, station, day)
    now = end + timedelta(hours=1)
    assert service.materialize_days(db, now) >= 1
    notice = db.scalar(select(Notification).where(Notification.station_id == station.id))
    assert [h["kind"] for h in notice.payload["highlights"]] == ["record", "independence"]
    assert "2 zile" in notice.payload["highlights"][0]["body"]
    notice.read_at = now
    db.flush()
    service.materialize_days(db, now + timedelta(minutes=1))
    assert (
        db.scalar(select(func.count(Notification.id)).where(Notification.station_id == station.id))
        == 1
    )
    current.pv_energy_kwh = Decimal(0)
    current.grid_import_energy_kwh = Decimal("2.5")
    db.flush()
    service.materialize_days(db, now + timedelta(minutes=2))
    db.refresh(notice)
    assert notice.read_at == now and notice.payload["highlights"] == []
    assert Decimal(notice.payload["metrics"][0]["value"]) == 0


def test_missing_day_stays_unknown_and_daily_cards_are_in_app_only(db, context):
    user, org, station, device, at = context
    now = at.replace(hour=12)
    service.materialize_days(db, now)
    notice = db.scalar(select(Notification).where(Notification.station_id == station.id))
    assert notice.payload["quality"] == "missing"
    assert all(m["value"] is None for m in notice.payload["metrics"])
    pref = notifications.preference(db, user, org)
    pref.matrix = {"info:info:email": "immediate"}
    pref.quiet_start = pref.quiet_end = 0
    pref.weekly_report = True
    notice.routed_at = None
    db.flush()
    notifications.route_pending(db, now)
    notifications.weekly_reports(db, now + timedelta(days=(7 - now.weekday()) % 7))
    assert all(
        str(notice.id) not in d.notification_ids for d in db.scalars(select(NotificationDelivery))
    )


def test_feed_filters_read_rbac_csrf_and_archive(db, client, context):
    user, org, station, device, at = context
    other_user = make_user(db, email="other-notices@test.local")
    make_membership(db, other_user, org, "viewer")
    service.materialize_days(db, at.replace(hour=12))
    sample(db, station, device, at, diagnostics={"battery": {"temperature_c": "80"}})
    health.evaluate_station(db, station, at)
    notifications.materialize(db, at)
    db.commit()
    assert login(client, user.email, "TestPass1234").status_code == 303
    endpoint = f"/stations/{station.id}/notifications"
    response = client.get(endpoint)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    feed = response.json()
    assert feed["unread_count"] == len(feed["items"]) >= 2
    summary = client.get(endpoint + "?kind=summary").json()["items"]
    assert len(summary) == 1 and summary[0]["payload"]["kind"] == "summary"
    assert all(
        i["payload"]["kind"] == "alert"
        for i in client.get(endpoint + "?kind=alert").json()["items"]
    )
    assert client.get(endpoint + "?kind=invalid").status_code == 422
    target = f"{endpoint}/{summary[0]['id']}/read"
    assert client.post(target).status_code == 403
    headers = {"X-CSRF-Token": client.cookies.get("ems_csrf")}
    assert client.post(target, headers=headers).json() == {"read": True}
    assert client.post(target, headers=headers).status_code == 200
    assert client.get(endpoint + "?kind=summary&unread=true").json()["items"] == []
    assert (
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "notification.read", AuditLog.actor_user_id == user.id
            )
        )
        == 1
    )
    other_notice = db.scalar(select(Notification).where(Notification.user_id == other_user.id))
    assert client.post(f"{endpoint}/{other_notice.id}/read", headers=headers).status_code == 404
    other_org = make_org(db, "Other notification organization")
    foreign_station = make_station(db, other_org, other_user)
    db.commit()
    assert client.get(f"/stations/{foreign_station.id}/notifications").status_code == 403
    org.status = "archived"
    db.commit()
    assert client.get(endpoint).status_code == 403
    org.status = "active"
    member = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.organization_id == org.id
        )
    )
    member.is_active = False
    db.commit()
    assert client.get(endpoint).status_code == 403


def test_voltage_incident_uses_fresh_measured_grid_phase_and_original_evidence(db, context):
    user, org, station, device, at = context
    sample(
        db,
        station,
        device,
        at,
        diagnostics={
            "phases": [
                {"phase": "L1", "circuit": "grid", "voltage_v": "262.1", "quality": "measured"},
                {"phase": "L2", "circuit": "load", "voltage_v": "270", "quality": "measured"},
                {"phase": "L3", "circuit": "grid", "voltage_v": "280", "quality": "simulated"},
            ]
        },
    )
    health.evaluate_station(db, station, at)
    alerts = db.scalars(
        select(Alert).where(Alert.station_id == station.id, Alert.category == "grid_voltage_high")
    ).all()
    assert len(alerts) == 1 and alerts[0].context["phase"] == "L1"
    # Materializing after another evaluation must retain the original event's value.
    sample(
        db,
        station,
        device,
        at + timedelta(minutes=5),
        diagnostics={"phases": [{"phase": "L1", "voltage_v": "280", "quality": "measured"}]},
    )
    health.evaluate_station(db, station, at + timedelta(minutes=5))
    notifications.materialize(db, at + timedelta(minutes=5))
    notice = db.scalar(
        select(Notification).where(
            Notification.title == "Tensiune ridicata in retea", Notification.user_id == user.id
        )
    )
    assert "262.1 V" in notice.payload["body"] and "253 V" in notice.payload["body"]
    assert "280 V" not in notice.payload["body"]
    for minute, voltage in [(10, "250"), (15, "248"), (20, "247")]:
        sample(
            db,
            station,
            device,
            at + timedelta(minutes=minute),
            diagnostics={"phases": [{"phase": "L1", "voltage_v": voltage, "quality": "measured"}]},
        )
        health.evaluate_station(db, station, at + timedelta(minutes=minute))
        assert alerts[0].status == (
            "resolved" if minute == 20 else "active" if minute == 10 else "resolving"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"is_simulated": True},
        {"quality_flags": {"stale": True}},
        {"quality_flags": {"derived": True}},
    ],
)
def test_untrusted_raw_voltage_does_not_create_an_incident(db, context, changes):
    user, org, station, device, at = context
    sample(
        db,
        station,
        device,
        at,
        diagnostics={"phases": [{"phase": "L1", "voltage_v": "280"}]},
        **changes,
    )
    health.evaluate_station(db, station, at)
    assert (
        db.scalar(
            select(Alert).where(
                Alert.station_id == station.id, Alert.category == "grid_voltage_high"
            )
        )
        is None
    )


def test_notification_migration_preserves_null_zero_payload_and_read_state(
    db, context, monkeypatch
):
    user, org, station, device, at = context
    service.materialize_days(db, at.replace(hour=12))
    notice = db.scalar(select(Notification).where(Notification.user_id == user.id))
    notice.payload = {"metrics": [{"value": None}, {"value": "0"}, {"value": "13.7000"}]}
    notice.read_at = at
    sample(db, station, device, at, diagnostics={"battery": {"temperature_c": "80"}})
    health.evaluate_station(db, station, at)
    notifications.materialize(db, at)
    db.flush()
    saved = {
        n.id: (n.event_id, n.source_key, n.payload, n.read_at)
        for n in db.scalars(select(Notification))
    }
    spec = spec_from_file_location(
        "day_notification_migration",
        Path(__file__).parents[2] / "alembic/versions/d91e62b48c03_station_day_notifications.py",
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db.connection())))
    migration.downgrade()
    retained = db.execute(
        text("SELECT payload, read_at FROM legacy_day_notifications WHERE id=:id"),
        {"id": notice.id},
    ).one()
    assert retained == (notice.payload, at)
    assert (
        db.execute(text("SELECT count(*) FROM notifications WHERE event_id IS NULL")).scalar_one()
        == 0
    )
    migration.upgrade()
    restored = {
        n.id: (n.event_id, n.source_key, n.payload, n.read_at)
        for n in db.scalars(select(Notification).execution_options(populate_existing=True))
    }
    assert restored == saved


def test_inbox_pagination_pins_yesterday_and_counts_all_unread(db, context):
    user, org, station, device, at = context
    today = utcnow().astimezone(ZoneInfo(station.timezone)).date()
    for days_ago in range(1, 36):
        db.add(
            Notification(
                user_id=user.id,
                station_id=station.id,
                source_key=f"day:{today - timedelta(days=days_ago)}",
                payload={"kind": "summary"},
                title="Day",
                severity="info",
                category="info",
                link="/",
                created_at=at + timedelta(minutes=days_ago),
            )
        )
    db.flush()
    first = service.inbox(db, station, user)
    second = service.inbox(db, station, user, offset=30)
    assert first["unread_count"] == 35 and first["has_more"]
    assert len(first["items"]) == 30 and len(second["items"]) == 5
    assert not second["has_more"]
    ids = [item["id"] for page in (first, second) for item in page["items"]]
    assert len(set(ids)) == 35
    pinned = db.get(Notification, UUID(ids[0]))
    assert pinned.source_key == f"day:{today - timedelta(days=1)}"


def test_daily_worker_concurrency_deduplicates_per_recipient(engine):
    suffix = uuid4().hex[:8]
    now = utcnow().replace(hour=12)
    with Session(engine) as db:
        user = make_user(db, email=f"daily-{suffix}@test.local")
        org = make_org(db)
        make_membership(db, user, org)
        station = make_station(db, org, user)
        station.created_at = now - timedelta(days=10)
        db.commit()
        user_id, org_id, station_id = user.id, org.id, station.id
    locked, release = Event(), Event()

    def create(hold=False):
        with Session(engine) as db:
            service.materialize_days(db, now)
            if hold:
                locked.set()
                assert release.wait(10)
            db.commit()

    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(create, True)
            assert locked.wait(10)
            second = pool.submit(create)
            release.set()
            first.result(10)
            second.result(10)
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count(Notification.id)).where(
                        Notification.station_id == station_id, Notification.user_id == user_id
                    )
                )
                == 1
            )
    finally:
        release.set()
        with Session(engine) as db:
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
