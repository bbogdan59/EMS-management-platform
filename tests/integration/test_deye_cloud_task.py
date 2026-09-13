"""Teste pentru `deye_cloud_poll_task` (issue #43), acelasi tipar ca
`test_admin_job_tasks.py`: taskul foloseste `session_scope()` intern (o
sesiune reala, separata de fixture-ul `db` obisnuit izolat prin SAVEPOINT),
deci testele folosesc `engine` direct, cu commit real si curatare explicita."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.core.security import utcnow
from app.models.device import Device
from app.models.deye_integration import DeyeCloudConnection
from app.models.enums import DeviceStatus, DeyeCloudConnectionStatus, TelemetrySource
from app.models.telemetry import TelemetryRaw
from app.services import deye_cloud_service as svc
from app.workers.tasks import deye_cloud_poll_task
from tests.factories import make_org, make_station, make_user


def _session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _cleanup(engine, *, org_id, user_id):
    from app.models.organization import Organization
    from app.models.user import User

    Session = _session_factory(engine)
    db = Session()
    try:
        db.execute(delete(Organization).where(Organization.id == org_id))
        db.commit()
    finally:
        db.close()
    db2 = Session()
    try:
        db2.execute(delete(User).where(User.id == user_id))
        db2.commit()
    finally:
        db2.close()


def _setup(engine, suffix: str):
    Session = _session_factory(engine)
    db = Session()
    try:
        user = make_user(db, email=f"deyetask-{suffix}-{uuid.uuid4().hex[:6]}@test.local")
        org = make_org(db, f"Deye Task Org {suffix}")
        station = make_station(db, org, user, name=f"Deye Task Station {suffix}")
        device = Device(
            station_id=station.id, name="Deye Cloud (test)", status=DeviceStatus.active.value,
            capabilities={"deye_cloud": True, "read_only": True},
        )
        db.add(device)
        db.flush()
        conn = DeyeCloudConnection(
            station_id=station.id,
            created_by_user_id=user.id,
            status=DeyeCloudConnectionStatus.connected.value,
            account_email="client@example.com",
            encrypted_account_password="unused-in-this-test",
            device_id=device.id,
            remote_station_id=322,
            remote_station_name="Casa Test",
            consent_accepted_at=utcnow(),
        )
        db.add(conn)
        db.commit()
        return user.id, org.id, station.id, device.id, conn.id
    finally:
        db.close()


def test_deye_cloud_poll_task_ingests_telemetry_for_connected_connection(engine, monkeypatch):
    user_id, org_id, station_id, device_id, conn_id = _setup(engine, "ok")
    monkeypatch.setattr(svc, "_valid_access_token", lambda connection: "token")
    monkeypatch.setattr(
        svc, "fetch_station_latest",
        lambda token, remote_station_id: {"generationPower": 2.5, "lastUpdateTime": int(utcnow().timestamp())},
    )
    try:
        result = deye_cloud_poll_task()
        assert result["succeeded"] == 1

        Session = _session_factory(engine)
        verify = Session()
        try:
            row = verify.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == device_id))
            assert row is not None
            assert row.source == TelemetrySource.deye_cloud.value
            conn = verify.get(DeyeCloudConnection, conn_id)
            assert conn.last_sync_status == "succeeded"
        finally:
            verify.close()
    finally:
        _cleanup(engine, org_id=org_id, user_id=user_id)


def test_deye_cloud_poll_task_ignores_disconnected_connection(engine, monkeypatch):
    user_id, org_id, station_id, device_id, conn_id = _setup(engine, "disc")
    Session = _session_factory(engine)
    setup2 = Session()
    try:
        conn = setup2.get(DeyeCloudConnection, conn_id)
        conn.status = DeyeCloudConnectionStatus.disconnected.value
        setup2.commit()
    finally:
        setup2.close()

    def _boom(*a, **k):
        raise AssertionError("nu ar trebui apelat pentru o conexiune deconectata")

    monkeypatch.setattr(svc, "fetch_station_latest", _boom)
    try:
        result = deye_cloud_poll_task()
        assert result["succeeded"] == 0

        verify = Session()
        try:
            count = verify.scalar(select(TelemetryRaw.id).where(TelemetryRaw.device_id == device_id))
            assert count is None
        finally:
            verify.close()
    finally:
        _cleanup(engine, org_id=org_id, user_id=user_id)


def test_deye_cloud_poll_task_one_connection_failure_does_not_block_others(engine, monkeypatch):
    user_id1, org_id1, station_id1, device_id1, conn_id1 = _setup(engine, "fail")
    user_id2, org_id2, station_id2, device_id2, conn_id2 = _setup(engine, "ok2")

    def _fetch(token, remote_station_id):
        return {"generationPower": 1.0, "lastUpdateTime": int(utcnow().timestamp())}

    call_count = {"n": 0}

    def _valid_token(connection):
        call_count["n"] += 1
        if connection.id == conn_id1:
            raise RuntimeError("eroare neasteptata la aceasta conexiune")
        return "token"

    monkeypatch.setattr(svc, "_valid_access_token", _valid_token)
    monkeypatch.setattr(svc, "fetch_station_latest", _fetch)
    try:
        result = deye_cloud_poll_task()
        assert result["succeeded"] == 1
        assert result["failed"] == 1

        Session = _session_factory(engine)
        verify = Session()
        try:
            row2 = verify.scalar(select(TelemetryRaw).where(TelemetryRaw.device_id == device_id2))
            assert row2 is not None
        finally:
            verify.close()
    finally:
        _cleanup(engine, org_id=org_id1, user_id=user_id1)
        _cleanup(engine, org_id=org_id2, user_id=user_id2)


def test_deye_cloud_poll_task_skips_when_local_device_recently_active(engine, monkeypatch):
    user_id, org_id, station_id, device_id, conn_id = _setup(engine, "local")
    Session = _session_factory(engine)
    setup2 = Session()
    try:
        local_device = Device(station_id=station_id, name="EMS local", status=DeviceStatus.active.value)
        setup2.add(local_device)
        setup2.flush()
        setup2.add(
            TelemetryRaw(
                device_id=local_device.id, station_id=station_id, boot_id="boot-1", sequence=1,
                measured_at=utcnow() - timedelta(minutes=1), received_at=utcnow(),
                source=TelemetrySource.device_rs485.value,
            )
        )
        setup2.commit()
    finally:
        setup2.close()

    def _boom(*a, **k):
        raise AssertionError("nu ar trebui apelat cand dispozitivul local e activ")

    monkeypatch.setattr(svc, "fetch_station_latest", _boom)
    try:
        result = deye_cloud_poll_task()
        assert result["skipped"] == 1

        verify = Session()
        try:
            row = verify.scalar(select(TelemetryRaw.id).where(TelemetryRaw.device_id == device_id))
            assert row is None
        finally:
            verify.close()
    finally:
        _cleanup(engine, org_id=org_id, user_id=user_id)
