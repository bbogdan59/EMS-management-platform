from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.notification import NotificationDelivery
from app.models.organization import Organization
from app.models.station import Station
from app.models.telemetry import TelemetryRaw
from app.models.user import User
from app.services import health_service as health
from app.services import notification_service as notifications
from tests.factories import make_device, make_membership, make_org, make_station, make_user


def test_workers_serialize_health_and_claim_each_delivery_once(engine, monkeypatch):
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:8]
    at = utcnow().replace(second=0, microsecond=0)
    at = at.replace(minute=at.minute // 5 * 5)
    with Session() as db:
        user = make_user(db, email=f"health-{suffix}@test.local")
        org = make_org(db, f"Health {suffix}")
        make_membership(db, user, org, "organization_admin")
        station = make_station(db, org, user)
        device = make_device(db, station)
        device.last_heartbeat_at = at
        db.add(
            TelemetryRaw(
                station_id=station.id,
                device_id=device.id,
                boot_id="concurrency",
                sequence=1,
                measured_at=at,
                received_at=at,
                diagnostics={"battery": {"quality": "measured", "temperature_c": "80"}},
            )
        )
        pref = notifications.preference(db, user, org)
        pref.verified_email = user.email
        pref.matrix = {"incident:critical:email": "immediate"}
        pref.escalation_minutes = 0
        db.commit()
        org_id, user_id, station_id = org.id, user.id, station.id
    locked, release = Event(), Event()

    def evaluate(hold=False):
        with Session() as db:
            count = health.evaluate_station(db, db.get(Station, station_id), at)
            if hold:
                locked.set()
                assert release.wait(10)
            db.commit()
            return count

    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(evaluate, True)
            assert locked.wait(10)
            second = pool.submit(evaluate)
            release.set()
            counts = [first.result(10), second.result(10)]
        assert counts[0] > 0 and counts[1] == 0
        with Session() as db:
            assert (
                db.scalar(
                    select(func.count(Alert.id)).where(
                        Alert.station_id == station_id, Alert.category == "battery_temperature"
                    )
                )
                == 1
            )
            notifications.materialize(db, at)
            notifications.route_pending(db, at)
            db.commit()
        monkeypatch.setattr(get_settings(), "notifications_email_enabled", True)
        sent = []
        send_started, release_send = Event(), Event()

        class Adapter:
            def send(self, *args):
                sent.append(True)
                send_started.set()
                assert release_send.wait(10)

        def deliver():
            with Session() as db:
                result = notifications.deliver_one(
                    db, at + timedelta(seconds=1), email_adapter=Adapter()
                )
                db.commit()
                return result

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(deliver)
            assert send_started.wait(10)
            second = pool.submit(deliver)
            assert second.result(10) is False
            release_send.set()
            assert first.result(10) is True
        assert len(sent) == 1
        with Session() as db:
            assert (
                db.scalar(
                    select(func.count(NotificationDelivery.id)).where(
                        NotificationDelivery.organization_id == org_id,
                        NotificationDelivery.status == "delivered",
                    )
                )
                == 1
            )
    finally:
        release.set()
        with Session() as db:
            db.execute(delete(AuditLog).where(AuditLog.organization_id == org_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()


def test_backfill_does_not_deadlock_with_foreign_key_lock_from_new_upload(engine):
    from sqlalchemy.dialects.postgresql import insert

    from app.models.telemetry import TelemetryBackfill
    from app.services.aggregation_service import drain_backfill

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:8]
    at = (utcnow() - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    with Session() as db:
        user = make_user(db, email=f"backfill-{suffix}@test.local")
        org = make_org(db, f"Backfill {suffix}")
        station = make_station(db, org, user)
        device = make_device(db, station)
        db.add(TelemetryBackfill(station_id=station.id, hour_start=at))
        db.commit()
        org_id, user_id, station_id, device_id = org.id, user.id, station.id, device.id
    queue_locked, raw_inserted = Event(), Event()

    def aggregate():
        with Session() as db:
            db.scalar(
                select(TelemetryBackfill)
                .where(TelemetryBackfill.station_id == station_id)
                .with_for_update()
            )
            queue_locked.set()
            assert raw_inserted.wait(10)
            drain_backfill(db)
            db.commit()

    def ingest():
        with Session() as db:
            assert queue_locked.wait(10)
            db.add(
                TelemetryRaw(
                    station_id=station_id,
                    device_id=device_id,
                    boot_id="concurrent-backfill",
                    sequence=1,
                    measured_at=at,
                    received_at=utcnow(),
                    pv_power_w=1000,
                )
            )
            db.flush()
            raw_inserted.set()
            db.execute(
                insert(TelemetryBackfill)
                .values(station_id=station_id, hour_start=at)
                .on_conflict_do_update(
                    constraint="uq_telemetry_backfill_hour", set_={"updated_at": utcnow()}
                )
            )
            db.commit()

    try:
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(aggregate), pool.submit(ingest)
            first.result(15)
            second.result(15)
        with Session() as db:
            # The newly committed sample must still get a subsequent rebuild.
            assert (
                db.scalar(
                    select(func.count(TelemetryBackfill.id)).where(
                        TelemetryBackfill.station_id == station_id
                    )
                )
                == 1
            )
            assert drain_backfill(db) == 1
            db.commit()
    finally:
        with Session() as db:
            db.execute(delete(AuditLog).where(AuditLog.organization_id == org_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
