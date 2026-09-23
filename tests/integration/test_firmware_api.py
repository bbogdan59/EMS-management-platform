"""API/route-level tests for issue #168: enrollment/heartbeat typed
inventory fields, device-facing firmware endpoints, admin release/rollout
routes and RBAC."""
from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.config import get_settings
from app.core.rate_limit import reset_key
from app.models.device import Device
from app.models.firmware import FirmwareRelease, FirmwareRollout
from app.services import device_service, firmware_service, firmware_storage
from tests.factories import make_device, make_org, make_station, make_user
from tests.web_helpers import login

_counter = [0]


def _uuid() -> str:
    return str(uuid.uuid4())


def _admin(db):
    _counter[0] += 1
    return make_user(db, email=f"fwapi{_counter[0]}@test.local", is_platform_admin=True, password="Password1234")


def _claim_code(db, station, user):
    claim, raw_code = device_service.create_claim_code(db, station, user)
    db.commit()
    return raw_code


@pytest.fixture()
def signing_key(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(get_settings(), "firmware_signing_private_key_pem", pem)
    monkeypatch.setattr(get_settings(), "firmware_signing_key_id", "test-key-1")
    return key


class _FakeStorage(firmware_storage.ReleaseStorage):
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put(self, key, data, *, content_type="application/octet-stream"):
        self.data[key] = data

    def generate_download_url(self, key, *, ttl_seconds):
        return f"fake://{key}?ttl={ttl_seconds}"

    def delete(self, key):
        self.data.pop(key, None)


# --- Enrollment / heartbeat typed inventory fields --------------------------


def test_enroll_persists_typed_inventory_fields(client, db):
    resp = client.post(
        "/api/v1/devices/enroll",
        json={
            "installation_uuid": _uuid(), "provisioning_secret": "s" * 20,
            "agent_version": "1.2.3", "build_id": "abc123", "hardware_platform": "raspberry-pi-4",
            "architecture": "arm64", "os_version": "Raspberry Pi OS Lite 12",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"

    device = db.scalar(select(Device).where(Device.firmware_version == "1.2.3"))
    assert device is not None
    assert device.build_id == "abc123"
    assert device.hardware_platform == "raspberry-pi-4"
    assert device.architecture == "arm64"
    assert device.os_version == "Raspberry Pi OS Lite 12"
    assert device.firmware_version_source == "enrollment"
    assert device.last_seen_at is not None


def test_enroll_idempotent_update_does_not_touch_allocation(client, db):
    installation_uuid = _uuid()
    secret = "s" * 20
    client.post("/api/v1/devices/enroll", json={"installation_uuid": installation_uuid, "provisioning_secret": secret})
    resp = client.post(
        "/api/v1/devices/enroll",
        json={
            "installation_uuid": installation_uuid, "provisioning_secret": secret,
            "agent_version": "2.0.0",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    assert device.firmware_version == "2.0.0"
    assert device.station_id is None


def test_heartbeat_persists_typed_inventory_fields(client, db):
    user = make_user(db, email="hbfields@test.local", password="Password1234")
    org = make_org(db, "Heartbeat Fields Org")
    station = make_station(db, org, user, name="Heartbeat Fields Station")
    db.commit()
    raw_code = _claim_code(db, station, user)
    claim = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}})
    payload = claim.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    resp = client.post(
        "/api/v1/devices/heartbeat",
        json={
            "boot_id": "b1", "firmware_version": "1.5.0", "capabilities": {},
            "build_id": "def456", "hardware_platform": "raspberry-pi-4", "architecture": "arm64",
            "os_version": "Raspberry Pi OS Lite 12",
        },
        headers=auth_header,
    )
    assert resp.status_code == 200
    device = db.get(Device, uuid.UUID(payload["device_id"]))
    assert device.firmware_version == "1.5.0"
    assert device.firmware_version_source == "heartbeat"
    assert device.build_id == "def456"
    assert device.last_seen_at is not None


# --- Device-facing firmware endpoints ---------------------------------------


def _active_device(client, db):
    user = make_user(db, email=f"fwdev{_counter[0]}@test.local", password="Password1234")
    _counter[0] += 1
    org = make_org(db, f"FW Device Org {_counter[0]}")
    station = make_station(db, org, user, name=f"FW Device Station {_counter[0]}")
    db.commit()
    raw_code = _claim_code(db, station, user)
    claim = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}})
    payload = claim.json()
    device = db.get(Device, uuid.UUID(payload["device_id"]))
    device.firmware_version = "1.0.0"
    device.hardware_platform = "raspberry-pi-4"
    device.architecture = "arm64"
    db.add(device)
    db.commit()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}
    return device, auth_header


def test_get_pending_firmware_offer_returns_null_when_none(client, db):
    _device, auth_header = _active_device(client, db)
    resp = client.get("/api/v1/firmware/pending", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_pending_firmware_offer_and_event_roundtrip(client, db, signing_key):
    device, auth_header = _active_device(client, db)
    admin = _admin(db)
    storage = _FakeStorage()
    release = firmware_service.create_release(
        db, admin, storage=storage, version="1.1.0", channel="stable", hardware_platform="raspberry-pi-4",
        architecture="arm64", protocol_schema_version=1, artifact_bytes=b"fake-bytes",
    )
    firmware_service.publish_release(db, release)
    firmware_service.create_rollout(db, admin, release, [device])
    db.commit()

    resp = client.get("/api/v1/firmware/pending", headers=auth_header)
    assert resp.status_code == 200
    offer = resp.json()
    assert offer["target_version"] == "1.1.0"
    assert offer["status"] == "offered"
    deployment_id = offer["deployment_id"]

    step1 = client.post(
        f"/api/v1/firmware/deployments/{deployment_id}/events",
        json={"event_type": "downloading", "payload": {}}, headers=auth_header,
    )
    assert step1.status_code == 200
    assert step1.json()["status"] == "downloading"

    invalid = client.post(
        f"/api/v1/firmware/deployments/{deployment_id}/events",
        json={"event_type": "installing", "payload": {}}, headers=auth_header,
    )
    assert invalid.status_code == 409


def test_report_firmware_event_requires_authentication(client, db):
    resp = client.post(
        f"/api/v1/firmware/deployments/{uuid.uuid4()}/events",
        json={"event_type": "downloading", "payload": {}},
    )
    assert resp.status_code == 401


# --- Admin routes: RBAC -----------------------------------------------------


def test_admin_firmware_releases_requires_platform_admin(client, db):
    reset_key("login_attempts:testclient")
    make_user(db, email="notadminfw1@test.local", password="Password1234")
    db.commit()
    login(client, "notadminfw1@test.local", "Password1234")
    resp = client.get("/admin/firmware/releases", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


def test_admin_firmware_rollouts_requires_platform_admin(client, db):
    reset_key("login_attempts:testclient")
    make_user(db, email="notadminfw2@test.local", password="Password1234")
    db.commit()
    login(client, "notadminfw2@test.local", "Password1234")
    resp = client.get("/admin/firmware/rollouts", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


# --- Admin routes: release create/publish/revoke ----------------------------


def test_admin_create_publish_revoke_release(client, db, signing_key):
    reset_key("login_attempts:testclient")
    admin = _admin(db)
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    create = client.post(
        "/admin/firmware/releases",
        data={
            "csrf_token": csrf, "version": "3.0.0", "channel": "stable", "hardware_platform": "raspberry-pi-4",
            "architecture": "arm64", "protocol_schema_version": "1", "min_compatible_agent_version": "",
            "release_notes": "First release",
        },
        files={"artifact": ("release.tar.gz", b"artifact-bytes", "application/gzip")},
        follow_redirects=False,
    )
    assert create.status_code == 303


    release = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "3.0.0"))
    assert release is not None
    assert release.status == "draft"

    publish = client.post(
        f"/admin/firmware/releases/{release.id}/publish", data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert publish.status_code == 303
    db.refresh(release)
    assert release.status == "published"

    revoke = client.post(
        f"/admin/firmware/releases/{release.id}/revoke",
        data={"csrf_token": csrf, "reason": "Security issue found"}, follow_redirects=False,
    )
    assert revoke.status_code == 303
    db.refresh(release)
    assert release.status == "revoked"


def test_admin_create_release_without_signing_key_shows_error(client, db):
    reset_key("login_attempts:testclient")
    admin = _admin(db)
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/firmware/releases",
        data={
            "csrf_token": csrf, "version": "4.0.0", "channel": "stable", "hardware_platform": "raspberry-pi-4",
            "architecture": "arm64", "protocol_schema_version": "1",
        },
        files={"artifact": ("release.tar.gz", b"artifact-bytes", "application/gzip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


# --- Admin routes: rollout preview + create + pause/resume/cancel ----------


def test_admin_rollout_preview_and_create_and_lifecycle(client, db, signing_key):
    reset_key("login_attempts:testclient")
    admin = _admin(db)
    org = make_org(db, "Admin Rollout Org")
    station = make_station(db, org, admin, name="Admin Rollout Station")
    device = make_device(db, station)
    device.firmware_version = "1.0.0"
    device.hardware_platform = "raspberry-pi-4"
    device.architecture = "arm64"
    db.add(device)
    db.commit()

    storage = _FakeStorage()
    release = firmware_service.create_release(
        db, admin, storage=storage, version="5.0.0", channel="stable", hardware_platform="raspberry-pi-4",
        architecture="arm64", protocol_schema_version=1, artifact_bytes=b"bytes",
    )
    firmware_service.publish_release(db, release)
    db.commit()

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    preview = client.post(
        "/admin/firmware/rollouts/preview",
        data={
            "csrf_token": csrf, "release_id": str(release.id), "device_id": str(device.id),
            "max_concurrent": "5", "failure_threshold_percent": "20",
        },
    )
    assert preview.status_code == 200
    assert "5.0.0" in preview.text
    assert device.name in preview.text

    create = client.post(
        "/admin/firmware/rollouts",
        data={
            "csrf_token": csrf, "release_id": str(release.id), "device_id": str(device.id),
            "max_concurrent": "5", "failure_threshold_percent": "20",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    rollout_url = create.headers["location"]

    detail = client.get(rollout_url)
    assert detail.status_code == 200
    assert "5.0.0" in detail.text

    rollout_id = rollout_url.rsplit("/", 1)[-1]
    pause = client.post(f"/admin/firmware/rollouts/{rollout_id}/pause", data={"csrf_token": csrf}, follow_redirects=False)
    assert pause.status_code == 303


    rollout = db.get(FirmwareRollout, uuid.UUID(rollout_id))
    assert rollout.status == "paused"

    resume = client.post(f"/admin/firmware/rollouts/{rollout_id}/resume", data={"csrf_token": csrf}, follow_redirects=False)
    assert resume.status_code == 303
    db.refresh(rollout)
    assert rollout.status == "active"

    cancel = client.post(
        f"/admin/firmware/rollouts/{rollout_id}/cancel",
        data={"csrf_token": csrf, "reason": "test cancel"}, follow_redirects=False,
    )
    assert cancel.status_code == 303
    db.refresh(rollout)
    assert rollout.status == "cancelled"


def test_fleet_page_shows_update_available_badge(client, db, signing_key):
    reset_key("login_attempts:testclient")
    admin = _admin(db)
    org = make_org(db, "Fleet Badge Org")
    station = make_station(db, org, admin, name="Fleet Badge Station")
    device = make_device(db, station)
    device.firmware_version = "1.0.0"
    device.hardware_platform = "raspberry-pi-4"
    device.architecture = "arm64"
    db.add(device)
    db.commit()

    storage = _FakeStorage()
    release = firmware_service.create_release(
        db, admin, storage=storage, version="6.0.0", channel="stable", hardware_platform="raspberry-pi-4",
        architecture="arm64", protocol_schema_version=1, artifact_bytes=b"bytes",
    )
    firmware_service.publish_release(db, release)
    firmware_service.create_rollout(db, admin, release, [device])
    db.commit()

    login(client, admin.email, "Password1234")
    resp = client.get("/admin/devices")
    assert resp.status_code == 200
    assert "update disponibil" in resp.text
