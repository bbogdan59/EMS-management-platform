from __future__ import annotations

import uuid
from datetime import timedelta

from app.core.security import utcnow
from app.services import device_service
from tests.factories import make_org, make_station, make_user


def _claim_code(db, station, user):
    claim, raw_code = device_service.create_claim_code(db, station, user)
    db.commit()
    return raw_code


def test_device_claim_and_telemetry_dedup(client, db):
    user = make_user(db, email="devowner1@test.local", password="Password1234")
    org = make_org(db, "Device Org 1")
    station = make_station(db, org, user, name="Device Station 1")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post(
        "/api/v1/devices/claim",
        json={"claim_code": raw_code, "device_name": "Test Device", "hardware_info": {}},
    )
    assert claim_resp.status_code == 201
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    now = utcnow().replace(microsecond=0)
    item = {
        "boot_id": "boot-1", "sequence": 1, "measured_at": now.isoformat(),
        "pv_power_w": 1000, "load_power_w": 500, "battery_power_w": -200, "grid_power_w": -300,
        "battery_soc_percent": 55.5, "ev_connected": False,
    }
    resp1 = client.post("/api/v1/telemetry/batch", json={"items": [item]}, headers=auth_header)
    assert resp1.status_code == 200
    assert resp1.json() == {"accepted": 1, "duplicates": 0, "rejected": 0, "errors": []}

    resp2 = client.post("/api/v1/telemetry/batch", json={"items": [item]}, headers=auth_header)
    assert resp2.json()["accepted"] == 0
    assert resp2.json()["duplicates"] == 1


def test_claim_code_cannot_be_reused(client, db):
    user = make_user(db, email="devowner2@test.local", password="Password1234")
    org = make_org(db, "Device Org 2")
    station = make_station(db, org, user, name="Device Station 2")
    db.commit()
    raw_code = _claim_code(db, station, user)

    first = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D1", "hardware_info": {}})
    assert first.status_code == 201

    second = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D2", "hardware_info": {}})
    assert second.status_code == 400


def test_expired_claim_code_rejected(client, db):
    user = make_user(db, email="devowner3@test.local", password="Password1234")
    org = make_org(db, "Device Org 3")
    station = make_station(db, org, user, name="Device Station 3")
    db.commit()

    claim, raw_code = device_service.create_claim_code(db, station, user)
    claim.expires_at = utcnow() - timedelta(minutes=1)
    db.add(claim)
    db.commit()

    resp = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}})
    assert resp.status_code == 400
    assert "expirat" in resp.json()["detail"].lower()


def test_device_revoke_blocks_further_access(client, db):
    user = make_user(db, email="devowner4@test.local", password="Password1234")
    org = make_org(db, "Device Org 4")
    station = make_station(db, org, user, name="Device Station 4")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}})
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    from app.models.device import Device
    device = db.get(Device, uuid.UUID(payload["device_id"]))
    device_service.revoke_device(db, device, "test revoke")
    db.commit()

    resp = client.post("/api/v1/devices/heartbeat", json={"boot_id": "b1", "firmware_version": "1", "capabilities": {}}, headers=auth_header)
    assert resp.status_code == 401
