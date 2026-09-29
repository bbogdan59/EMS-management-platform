import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.crypto import decrypt_secret
from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.models.audit import AuditLog
from app.models.command import Command
from app.models.control import Recommendation
from app.models.home_assistant import HomeAssistantConnection
from app.models.organization import Organization
from app.models.station import Station
from app.models.telemetry import TelemetryRaw
from app.models.user import User
from app.schemas.home_assistant import ConnectionInput
from app.services import home_assistant_mqtt as transport
from app.services import home_assistant_service as service
from app.services.home_assistant_worker import poll_connection
from tests.factories import make_device, make_membership, make_org, make_station, make_user
from tests.unit.test_home_assistant_contract import sample
from tests.web_helpers import login


@pytest.fixture(autouse=True)
def mqtt_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "home_assistant_mqtt_enabled", True)
    monkeypatch.setattr(
        get_settings(),
        "home_assistant_mqtt_brokers",
        {"test-broker": "mqtts://broker.example:8883"},
    )
    monkeypatch.setattr(transport, "exchange", lambda **kw: [])


def setup(db, client=None, role="organization_admin"):
    suffix = uuid4().hex[:8]
    user = make_user(db, email=f"ha-{suffix}@test.local")
    org = make_org(db, "HA " + suffix)
    make_membership(db, user, org, role)
    station = make_station(db, org, user)
    db.commit()
    if client:
        reset_key("login_attempts:testclient")
        login(client, user.email, "TestPass1234")
    return user, org, station


def config(revision=0, **changes):
    return ConnectionInput(
        **{
            "revision": revision,
            "broker_key": "test-broker",
            "username": "mqtt-user-private",
            "password": "mqtt-password-private",
            "consent": True,
            "publish_consent": True,
            "mappings": [{"entity_id": "sensor.boiler_power", "kind": "power", "unit": "W"}],
            **changes,
        }
    )


def submit(client, station, **changes):
    return client.post(
        f"/stations/{station.id}/integrations/home-assistant/configure",
        data={
            "csrf_token": client.cookies.get("ems_csrf"),
            "revision": 0,
            "broker_key": "test-broker",
            "username": "mqtt-user-private",
            "password": "mqtt-password-private",
            "consent": "yes",
            "publish_consent": "yes",
            "entity_id": "sensor.boiler_power",
            "kind": "power",
            "unit": "W",
            "max_age_seconds": 180,
            **changes,
        },
        follow_redirects=False,
    )


def test_opt_in_ui_stores_encrypted_secrets_and_never_calls_network_in_request(
    client, db, monkeypatch
):
    _, _, station = setup(db, client)
    monkeypatch.setattr(
        transport, "exchange", lambda **kw: pytest.fail("Network must run in worker only")
    )
    assert submit(client, station).status_code == 303
    connection = service.get_connection(db, station.id)
    assert connection.enabled and connection.status == "pending"
    assert "mqtt-password-private" not in connection.encrypted_credentials
    assert (
        json.loads(decrypt_secret(connection.encrypted_credentials))["password"]
        == "mqtt-password-private"
    )
    base = f"/stations/{station.id}/integrations/home-assistant"
    for path in (base, base + "/status", base + "/home-assistant.yaml"):
        response = client.get(path)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        assert (
            "mqtt-password-private" not in response.text
            and "mqtt-user-private" not in response.text
        )
    audit = db.scalars(select(AuditLog).where(AuditLog.station_id == station.id)).all()
    assert "mqtt-password-private" not in str([a.metadata_json for a in audit])
    assert (
        client.post(
            base + "/manage",
            data={"csrf_token": client.cookies.get("ems_csrf"), "revision": 1, "action": "test"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert service.get_connection(db, station.id).status == "test_pending"
    assert db.scalar(select(func.count(Command.id)).where(Command.station_id == station.id)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"consent": ""},
        {"broker_key": "mqtts://169.254.169.254"},
        {"entity_id": "person.private"},
        {"max_age_seconds": "0"},
        {"unit": "kWh"},
        {"password": ""},
    ],
)
def test_invalid_setup_does_not_persist_or_echo_credentials(client, db, changes):
    _, _, station = setup(db, client)
    response = submit(client, station, **changes)
    assert response.status_code == 422
    assert "mqtt-password-private" not in response.text
    assert service.get_connection(db, station.id) is None


def test_viewer_csrf_and_cross_tenant_protections(client, db):
    owner, org, station = setup(db)
    service.configure(db, station, owner, config())
    db.commit()
    viewer = make_user(db, email=f"viewer-{uuid4().hex}@test.local")
    make_membership(db, viewer, org, "viewer")
    db.commit()
    reset_key("login_attempts:testclient")
    login(client, viewer.email, "TestPass1234")
    base = f"/stations/{station.id}/integrations/home-assistant"
    assert client.get(base).status_code == 200
    assert 'id="ha-config-form"' not in client.get(base).text
    assert client.get(base + "/status").status_code == 200
    assert client.get(base + "/home-assistant.yaml").status_code == 403
    assert submit(client, station).status_code == 403
    assert (
        client.post(
            base + "/manage",
            data={"csrf_token": client.cookies.get("ems_csrf"), "revision": 1, "action": "revoke"},
        ).status_code
        == 403
    )
    login(client, owner.email, "TestPass1234")
    assert submit(client, station, csrf_token="forged").status_code == 403
    setup(db, client)
    assert client.get(base).status_code == 403
    assert client.get(base + "/status").status_code == 403
    assert submit(client, station).status_code == 403
    assert (
        client.post(
            base + "/manage",
            data={"csrf_token": client.cookies.get("ems_csrf"), "revision": 1, "action": "revoke"},
        ).status_code
        == 403
    )
    assert service.get_connection(db, station.id).enabled


def test_worker_contract_dedup_backoff_and_successful_reconnect(db, monkeypatch):
    user, _, station = setup(db)
    connection = service.configure(db, station, user, config())
    mapping = service.mappings_for(db, connection)[0]
    now = utcnow() + timedelta(seconds=1)
    body = json.dumps(sample(observed_at=now.isoformat(), value=0)).encode()
    seen = []

    def exchange(**kwargs):
        seen.append(kwargs)
        return [(service.context_topic(connection, mapping), body), ("another/station", body)]

    monkeypatch.setattr(transport, "exchange", exchange)
    result = poll_connection(db, connection.id, now=now)
    assert result == {"status": "connected", "accepted": 1, "rejected": 1}
    assert seen[0]["topics"] == [service.context_topic(connection, mapping)]
    assert seen[0]["publication"][1]["capabilities"]["physical_control"] is False
    assert service.observation(mapping, now=now)["value"] == "0"
    assert poll_connection(db, connection.id, now=now + timedelta(seconds=31))["accepted"] == 0
    assert mapping.received_at == now

    def fail(**kwargs):
        raise transport.MQTTFailure("auth_failed")

    monkeypatch.setattr(transport, "exchange", fail)
    later = now + timedelta(minutes=2)
    assert poll_connection(db, connection.id, now=later)["error_code"] == "auth_failed"
    assert connection.failure_count == 1 and connection.next_attempt_at > later
    assert poll_connection(db, connection.id, now=later + timedelta(seconds=1)) == {
        "skipped": "backoff"
    }
    monkeypatch.setattr(transport, "exchange", exchange)
    reconnect = later + timedelta(minutes=4)
    assert poll_connection(db, connection.id, now=reconnect)["status"] == "connected"
    assert connection.failure_count == 0 and connection.error_code is None
    assert service.observation(mapping, now=reconnect)["value"] is None
    assert (
        db.scalar(select(func.count(TelemetryRaw.id)).where(TelemetryRaw.station_id == station.id))
        == 0
    )
    assert db.scalar(select(func.count(Command.id)).where(Command.station_id == station.id)) == 0


def test_rotation_revoke_and_remapping_fence_old_messages(db, monkeypatch):
    user, _, station = setup(db)
    connection = service.configure(db, station, user, config())
    old_mapping = service.mappings_for(db, connection)[0]
    old_topic = service.context_topic(connection, old_mapping)
    old_cipher = connection.encrypted_credentials
    service.manage(db, station, user, "rotate", 1, username="new", password="rotated")
    assert service.context_topic(connection, old_mapping) == old_topic
    assert connection.encrypted_credentials != old_cipher
    assert service.credentials(connection)["password"] == "rotated"
    with pytest.raises(ValueError, match="modificata"):
        service.configure(db, station, user, config(1))
    connection = service.configure(db, station, user, config(2, publish_consent=False))
    new_maps = service.mappings_for(db, connection)
    assert service.context_topic(connection, new_maps[0]) != old_topic
    assert (
        service.ingest(connection, new_maps, old_topic, json.dumps(sample()).encode()) == "rejected"
    )
    service.manage(db, station, user, "revoke", 3)
    assert not connection.enabled and connection.encrypted_credentials is None
    assert not service.mappings_for(db, connection) and connection.consent_at is None
    assert poll_connection(db, connection.id)["skipped"] == "disabled_or_busy"
    assert service.ingest(connection, new_maps, old_topic, b"{}") == "disabled"


def test_disabled_integration_broker_removal_and_decryption_errors_are_independent_of_core(
    db, client, monkeypatch
):
    user, _, station = setup(db, client)
    connection = service.configure(db, station, user, config())
    monkeypatch.setattr(get_settings(), "home_assistant_mqtt_enabled", False)
    assert poll_connection(db, connection.id) == {"skipped": "disabled"}
    assert client.get("/health").status_code == 200
    assert client.get(f"/?station_id={station.id}").status_code == 200
    assert (
        client.get(f"/stations/{station.id}/integrations/home-assistant/status").json()["enabled"]
        is False
    )
    monkeypatch.setattr(get_settings(), "home_assistant_mqtt_enabled", True)
    monkeypatch.setattr(get_settings(), "home_assistant_mqtt_brokers", {})
    assert poll_connection(db, connection.id)["error_code"] == "broker_disabled"
    monkeypatch.setattr(
        get_settings(),
        "home_assistant_mqtt_brokers",
        {"test-broker": "mqtts://broker.example:8883"},
    )
    connection.encrypted_credentials = "corrupt"
    connection.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.flush()
    assert poll_connection(db, connection.id)["error_code"] == "credentials_unreadable"


def test_publication_is_station_scoped_and_requires_separate_consent(db, monkeypatch):
    user, _, station = setup(db)
    other_user, _, other_station = setup(db)
    connection = service.configure(db, station, user, config(publish_consent=False))
    now = utcnow()
    for s, expiry, title in (
        (station, now + timedelta(hours=1), "Ours"),
        (station, now - timedelta(seconds=1), "Expired"),
        (other_station, now + timedelta(hours=1), "Private other tenant"),
    ):
        db.add(
            Recommendation(
                station_id=s.id,
                fingerprint=uuid4().hex,
                kind="preset",
                status="available",
                snapshot={
                    "title": title,
                    "confidence": "insufficient_data",
                    "secret": "do-not-export",
                },
                expires_at=expiry,
            )
        )
    db.flush()
    pub = service.publication(db, connection, now=now)
    assert len(pub["recommendations"]) == 1 and pub["recommendations"][0]["title"] == "Ours"
    assert "do-not-export" not in str(pub) and "Private other tenant" not in str(pub)

    def exchange(**kwargs):
        assert kwargs["publication"] is None
        return []

    monkeypatch.setattr(transport, "exchange", exchange)
    assert poll_connection(db, connection.id)["status"] == "connected"
    assert connection.last_published_at is None


def test_context_retention_erases_values_but_keeps_replay_watermark(db):
    user, _, station = setup(db)
    connection = service.configure(db, station, user, config())
    mapping = service.mappings_for(db, connection)[0]
    mapping.value, mapping.available, mapping.quality = "0", True, "measured"
    mapping.observed_at = utcnow() - timedelta(hours=25)
    db.flush()
    assert service.purge_context(db) >= 1
    db.refresh(mapping)
    assert mapping.value is None and not mapping.available and mapping.observed_at is not None


def test_disabled_scheduler_still_erases_old_private_context_without_network(db, monkeypatch):
    from app.workers import tasks

    user, _, station = setup(db)
    connection = service.configure(db, station, user, config())
    mapping = service.mappings_for(db, connection)[0]
    mapping.value, mapping.available, mapping.quality = "2", True, "measured"
    mapping.observed_at = utcnow() - timedelta(hours=25)
    db.flush()
    monkeypatch.setattr(get_settings(), "home_assistant_mqtt_enabled", False)
    monkeypatch.setattr(tasks, "_task_lock", lambda *a, **kw: nullcontext(True))

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(tasks, "session_scope", session)
    monkeypatch.setattr(
        tasks.home_assistant_poll_task,
        "delay",
        lambda *a: pytest.fail("Disabled connector must not contact broker"),
    )
    result = tasks.home_assistant_schedule_task.run()
    assert result["skipped"] == "disabled" and result["purged"] >= 1
    db.refresh(mapping)
    assert mapping.value is None


def test_migration_leaves_existing_null_and_nonnull_measurements_untouched(db, monkeypatch):
    user, _, station = setup(db)
    device = make_device(db, station)
    for i, value in enumerate((None, 0, "1234.567")):
        db.add(
            TelemetryRaw(
                station_id=station.id,
                device_id=device.id,
                boot_id="migration-ha",
                sequence=i,
                measured_at=utcnow(),
                received_at=utcnow(),
                pv_power_w=value,
            )
        )
    db.flush()
    query = text("SELECT pv_power_w FROM telemetry_raw WHERE device_id=:id ORDER BY sequence")
    before = db.execute(query, {"id": device.id}).all()
    spec = spec_from_file_location(
        "ha_migration",
        Path(__file__).parents[2] / "alembic/versions/f189a27c803e_home_assistant_mqtt.py",
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(db.connection())))
    migration.downgrade()
    assert db.execute(query, {"id": device.id}).all() == before
    migration.upgrade()
    assert db.execute(query, {"id": device.id}).all() == before


def test_postgres_worker_lock_fences_revocation_and_parallel_poll(engine, monkeypatch):
    with Session(engine) as db:
        user, org, station = setup(db)
        connection = service.configure(db, station, user, config())
        db.commit()
        user_id, org_id, station_id, connection_id = user.id, org.id, station.id, connection.id
    locked, release = Event(), Event()

    def exchange(**kwargs):
        locked.set()
        assert release.wait(10)
        return []

    monkeypatch.setattr(transport, "exchange", exchange)

    def poll():
        with Session(engine) as db:
            result = poll_connection(db, connection_id)
            db.commit()
            return result

    def revoke():
        with Session(engine) as db:
            service.manage(db, db.get(Station, station_id), db.get(User, user_id), "revoke", 1)
            db.commit()

    try:
        with ThreadPoolExecutor(3) as pool:
            running = pool.submit(poll)
            assert locked.wait(10)
            assert pool.submit(poll).result(5) == {"skipped": "disabled_or_busy"}
            revoking = pool.submit(revoke)
            release.set()
            assert running.result(10)["status"] == "connected"
            revoking.result(10)
        assert poll() == {"skipped": "disabled_or_busy"}
        with Session(engine) as db:
            assert db.get(HomeAssistantConnection, connection_id).encrypted_credentials is None
    finally:
        release.set()
        with Session(engine) as db:
            db.execute(delete(AuditLog).where(AuditLog.station_id == station_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
