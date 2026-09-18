"""Compact per-device debug log: POST /api/v1/devices/logs, retention/prune
(DEVICE_LOG_RETENTION = 10 days), and its display on the device configuration
page (firmware, live system stats, log lines)."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.models.device import Device, DeviceLogEntry
from app.schemas.device_api import DeviceLogEntryIn
from app.services import device_service
from tests.factories import make_device, make_org, make_station, make_user
from tests.web_helpers import login


def _claim_code(db, station, user):
    claim, raw_code = device_service.create_claim_code(db, station, user)
    db.commit()
    return raw_code


def _entry(occurred_at, level="warning", code="sample_failed", detail="ValueError"):
    return DeviceLogEntryIn(occurred_at=occurred_at, level=level, code=code, detail=detail)


# --- Service level -----------------------------------------------------


def test_ingest_device_logs_stores_entries(db):
    user = make_user(db, email="devlogs1@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 1")
    station = make_station(db, org, user, name="Dev Logs Station 1")
    device = make_device(db, station)
    db.commit()

    now = utcnow()
    accepted = device_service.ingest_device_logs(db, device, [_entry(now)])
    db.commit()

    assert accepted == 1
    rows = list(db.scalars(select(DeviceLogEntry)))
    assert len(rows) == 1
    assert rows[0].code == "sample_failed"
    assert rows[0].device_id == device.id


def test_ingest_device_logs_drops_future_skew_entries(db):
    user = make_user(db, email="devlogs2@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 2")
    station = make_station(db, org, user, name="Dev Logs Station 2")
    device = make_device(db, station)
    db.commit()

    far_future = utcnow() + timedelta(hours=1)
    accepted = device_service.ingest_device_logs(db, device, [_entry(far_future)])
    db.commit()

    assert accepted == 0
    assert device_service.list_recent_device_logs(db, device) == []


def test_ingest_device_logs_prunes_entries_older_than_retention(db):
    """A row that was fresh when it landed becomes stale purely by the
    passage of time -- simulated here via a direct insert (as if ingested
    long ago), not by ingesting an already-old timestamp today."""
    user = make_user(db, email="devlogs3@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 3")
    station = make_station(db, org, user, name="Dev Logs Station 3")
    device = make_device(db, station)
    db.commit()

    now = utcnow()
    stale_at = now - device_service.DEVICE_LOG_RETENTION - timedelta(days=1)
    db.add(DeviceLogEntry(
        device_id=device.id, occurred_at=stale_at, received_at=stale_at,
        level="warning", code="old_event", detail=None,
    ))
    db.commit()

    # An unrelated new-entry ingest still prunes the now-stale row.
    device_service.ingest_device_logs(db, device, [_entry(now, code="new_event")])
    db.commit()

    remaining = device_service.list_recent_device_logs(db, device)
    assert [r.code for r in remaining] == ["new_event"]


def test_list_recent_device_logs_returns_newest_first(db):
    user = make_user(db, email="devlogs4@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 4")
    station = make_station(db, org, user, name="Dev Logs Station 4")
    device = make_device(db, station)
    db.commit()

    now = utcnow()
    device_service.ingest_device_logs(
        db, device,
        [_entry(now - timedelta(minutes=5), code="first"), _entry(now, code="second")],
    )
    db.commit()

    rows = device_service.list_recent_device_logs(db, device)
    assert [r.code for r in rows] == ["second", "first"]


# --- HTTP API level ------------------------------------------------------


def test_heartbeat_logs_endpoint_persists_compact_entries(client, db):
    user = make_user(db, email="devlogs5@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 5")
    station = make_station(db, org, user, name="Dev Logs Station 5")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post(
        "/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}}
    )
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    resp = client.post(
        "/api/v1/devices/logs",
        json={"entries": [
            {"occurred_at": utcnow().isoformat(), "level": "warning", "code": "cloud_http_error", "detail": "status=503"},
        ]},
        headers=auth_header,
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}

    device = db.get(Device, uuid.UUID(payload["device_id"]))
    rows = device_service.list_recent_device_logs(db, device)
    assert len(rows) == 1
    assert rows[0].code == "cloud_http_error"
    assert rows[0].detail == "status=503"


def test_heartbeat_logs_endpoint_requires_authentication(client):
    resp = client.post("/api/v1/devices/logs", json={"entries": []})
    assert resp.status_code == 401


def test_heartbeat_logs_endpoint_rejects_oversized_batch(client, db):
    user = make_user(db, email="devlogs6@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 6")
    station = make_station(db, org, user, name="Dev Logs Station 6")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post(
        "/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}}
    )
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    oversized = [
        {"occurred_at": utcnow().isoformat(), "level": "info", "code": "x"} for _ in range(51)
    ]
    resp = client.post("/api/v1/devices/logs", json={"entries": oversized}, headers=auth_header)
    assert resp.status_code == 422


def test_heartbeat_logs_endpoint_rejects_stack_trace_sized_detail(client, db):
    """`detail` is capped so a device can't ship a full stack trace/payload --
    this is a live-debugging aid, deliberately compact."""
    user = make_user(db, email="devlogs7@test.local", password="Password1234")
    org = make_org(db, "Dev Logs Org 7")
    station = make_station(db, org, user, name="Dev Logs Station 7")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post(
        "/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}}
    )
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    resp = client.post(
        "/api/v1/devices/logs",
        json={"entries": [
            {"occurred_at": utcnow().isoformat(), "level": "error", "code": "x", "detail": "x" * 500},
        ]},
        headers=auth_header,
    )
    assert resp.status_code == 422


# --- Configuration page ---------------------------------------------------


def test_configuration_page_shows_firmware_stats_and_log_lines(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="devlogspage1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Dev Logs Page Org 1")
    station = make_station(db, org, admin, name="Dev Logs Page Station 1")
    device = make_device(db, station)
    db.commit()

    device_service.record_heartbeat(
        db, device, "boot-1", "9.9.9", {}, {"cpu_load_1m": 0.11, "temperature_c": 42.0}
    )
    device_service.ingest_device_logs(db, device, [_entry(utcnow(), code="cloud_http_error", detail="status=503")])
    db.commit()

    login(client, admin.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/devices/{device.id}/configuration")
    assert resp.status_code == 200
    assert "9.9.9" in resp.text
    assert "0.11" in resp.text
    assert "42.0" in resp.text
    assert "cloud_http_error" in resp.text
    assert "status=503" in resp.text


def test_configuration_page_shows_empty_state_with_no_logs(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="devlogspage2@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Dev Logs Page Org 2")
    station = make_station(db, org, admin, name="Dev Logs Page Station 2")
    device = make_device(db, station)
    db.commit()

    login(client, admin.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/devices/{device.id}/configuration")
    assert resp.status_code == 200
    assert "Niciun eveniment" in resp.text
