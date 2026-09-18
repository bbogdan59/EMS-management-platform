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


def test_telemetry_contract_endpoint_is_public_and_declares_ack_semantics(client):
    resp = client.get("/api/v1/telemetry/contract")

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["endpoint"] == "/api/v1/telemetry/batch"
    assert body["deduplication_key"] == ["device_id", "boot_id", "sequence"]
    assert body["ack"]["statuses"] == ["accepted", "duplicate", "rejected"]
    assert "future_timestamp" in body["ack"]["retryable_reason_codes"]
    assert "timestamp_too_old" in body["ack"]["permanent_reason_codes"]


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
    assert resp1.json() == {
        "accepted": 1,
        "duplicates": 0,
        "rejected": 0,
        "errors": [],
        "results": [{
            "boot_id": "boot-1",
            "sequence": 1,
            "status": "accepted",
            "retryable": False,
            "reason_code": None,
        }],
    }

    resp2 = client.post("/api/v1/telemetry/batch", json={"items": [item]}, headers=auth_header)
    assert resp2.json()["accepted"] == 0
    assert resp2.json()["duplicates"] == 1
    assert resp2.json()["results"][0]["status"] == "duplicate"


def test_telemetry_batch_returns_ordered_per_item_ack_with_retryability(client, db):
    user = make_user(db, email="dev-ack@test.local", password="Password1234")
    org = make_org(db, "Device ACK Org")
    station = make_station(db, org, user, name="Device ACK Station")
    db.commit()
    raw_code = _claim_code(db, station, user)
    claimed = client.post(
        "/api/v1/devices/claim",
        json={"claim_code": raw_code, "device_name": "ACK Device", "hardware_info": {}},
    ).json()
    headers = {"Authorization": f"Bearer {claimed['device_id']}.{claimed['credential_secret']}"}
    now = utcnow().replace(microsecond=0)

    existing = {"boot_id": "ack-boot", "sequence": 1, "measured_at": now.isoformat()}
    assert client.post("/api/v1/telemetry/batch", json={"items": [existing]}, headers=headers).status_code == 200

    response = client.post(
        "/api/v1/telemetry/batch",
        json={"items": [
            existing,
            {"boot_id": "ack-boot", "sequence": 2, "measured_at": now.isoformat()},
            {"boot_id": "ack-boot", "sequence": 3, "measured_at": (now - timedelta(days=401)).isoformat()},
            {"boot_id": "ack-boot", "sequence": 4, "measured_at": (now + timedelta(minutes=6)).isoformat()},
            {"boot_id": "ack-boot", "sequence": 5, "measured_at": now.isoformat(), "pv_power_w": -1},
        ]},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["accepted"], body["duplicates"], body["rejected"]) == (1, 1, 3)
    assert [item["status"] for item in body["results"]] == [
        "duplicate", "accepted", "rejected", "rejected", "rejected"
    ]
    assert body["results"][2]["reason_code"] == "timestamp_too_old"
    assert body["results"][2]["retryable"] is False
    assert body["results"][3]["reason_code"] == "future_timestamp"
    assert body["results"][3]["retryable"] is True
    assert body["results"][4]["reason_code"] == "pv_power_negative"
    assert body["results"][4]["retryable"] is False
    assert len(body["errors"]) == 3  # camp legacy pastrat pentru clientii v1


def test_duplicate_keys_inside_one_batch_get_one_accepted_and_one_duplicate(client, db):
    user = make_user(db, email="dev-in-batch-dup@test.local", password="Password1234")
    org = make_org(db, "Device In-batch Duplicate Org")
    station = make_station(db, org, user, name="Device In-batch Duplicate Station")
    db.commit()
    raw_code = _claim_code(db, station, user)
    claimed = client.post(
        "/api/v1/devices/claim",
        json={"claim_code": raw_code, "device_name": "Duplicate Device", "hardware_info": {}},
    ).json()
    headers = {"Authorization": f"Bearer {claimed['device_id']}.{claimed['credential_secret']}"}
    item = {"boot_id": "same-batch", "sequence": 9, "measured_at": utcnow().isoformat()}

    response = client.post(
        "/api/v1/telemetry/batch", json={"items": [item, item]}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["accepted"], body["duplicates"], body["rejected"]) == (1, 1, 0)
    assert [result["status"] for result in body["results"]] == ["accepted", "duplicate"]


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


def test_heartbeat_persists_reported_system_stats(client, db):
    user = make_user(db, email="devowner5@test.local", password="Password1234")
    org = make_org(db, "Device Org 5")
    station = make_station(db, org, user, name="Device Station 5")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post(
        "/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}}
    )
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    resp = client.post(
        "/api/v1/devices/heartbeat",
        json={
            "boot_id": "b1", "firmware_version": "1", "capabilities": {},
            "system_stats": {"cpu_load_1m": 0.3, "memory_used_percent": 40, "disk_used_percent": 12},
        },
        headers=auth_header,
    )
    assert resp.status_code == 200

    from app.models.device import Device
    device = db.get(Device, uuid.UUID(payload["device_id"]))
    assert device.system_stats == {"cpu_load_1m": 0.3, "memory_used_percent": 40, "disk_used_percent": 12}


def test_heartbeat_without_system_stats_defaults_to_empty_not_missing(client, db):
    """Older agents that don't send `system_stats` at all must not 422 --
    the field is optional, defaulting to {}."""
    user = make_user(db, email="devowner6@test.local", password="Password1234")
    org = make_org(db, "Device Org 6")
    station = make_station(db, org, user, name="Device Station 6")
    db.commit()
    raw_code = _claim_code(db, station, user)

    claim_resp = client.post(
        "/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}}
    )
    payload = claim_resp.json()
    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}

    resp = client.post(
        "/api/v1/devices/heartbeat",
        json={"boot_id": "b1", "firmware_version": "1", "capabilities": {}},
        headers=auth_header,
    )
    assert resp.status_code == 200

    from app.models.device import Device
    device = db.get(Device, uuid.UUID(payload["device_id"]))
    assert device.system_stats == {}
