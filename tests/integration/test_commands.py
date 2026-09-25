from __future__ import annotations

from datetime import timedelta

from app.core.security import utcnow
from app.models.command import Command
from app.models.enums import CommandStatus, CommandType
from app.services import device_service
from tests.energy_helpers import command_for, ops_fixture  # noqa: F401
from tests.factories import make_org, make_station, make_user


def _claim(client, db, station, user):
    claim, raw_code = device_service.create_claim_code(db, station, user)
    db.commit()
    resp = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}})
    payload = resp.json()
    return payload, {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}


def test_command_full_lifecycle(client, db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    cmd = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    headers = {"Authorization": f"Bearer {device.id}.ops-secret"}
    db.commit()

    pending = client.get("/api/v1/commands/pending", headers=headers)
    assert pending.status_code == 200
    items = pending.json()
    assert len(items) == 1
    assert items[0]["command_id"] == str(cmd.id)

    db.refresh(cmd)
    assert cmd.status == CommandStatus.delivered.value

    ack = client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "accepted"}, headers=headers)
    assert ack.status_code == 200
    assert ack.json()["status"] == "accepted"

    result = client.post(
        f"/api/v1/commands/{cmd.id}/result",
        json={"status": "executed", "details": {"applied": True}},
        headers=headers,
    )
    assert result.status_code == 200
    assert result.json()["status"] == "executed"

    from sqlalchemy import select

    from app.models.command import CommandEvent

    events = db.scalars(select(CommandEvent).where(CommandEvent.command_id == cmd.id)).all()
    event_types = [e.event_type for e in events]
    assert "delivered" in event_types
    assert "accepted" in event_types
    assert "executed" in event_types


def test_command_rejection(client, db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    cmd = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    headers = {"Authorization": f"Bearer {device.id}.ops-secret"}
    db.commit()

    client.get("/api/v1/commands/pending", headers=headers)
    resp = client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "rejected", "reason": "Dispozitivul refuza."}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    # Nu se poate raporta rezultat pentru o comanda respinsa.
    result = client.post(f"/api/v1/commands/{cmd.id}/result", json={"status": "executed", "details": {}}, headers=headers)
    assert result.status_code == 409


def test_command_expires_and_is_not_deliverable(client, db):
    user = make_user(db, email="cmd3@test.local", password="Password1234")
    org = make_org(db, "Cmd Org 3")
    station = make_station(db, org, user, name="Cmd Station 3")
    db.commit()
    payload, headers = _claim(client, db, station, user)

    import uuid

    cmd = Command(
        station_id=station.id, device_id=uuid.UUID(payload["device_id"]),
        type=CommandType.hold_battery.value, parameters={},
        version=1, idempotency_key="test-key-3", status=CommandStatus.created.value,
        author="user", reason="Test expiry",
        valid_from=utcnow() - timedelta(minutes=20), expires_at=utcnow() - timedelta(minutes=5),
    )
    db.add(cmd)
    db.commit()

    pending = client.get("/api/v1/commands/pending", headers=headers)
    assert pending.json() == []

    db.refresh(cmd)
    assert cmd.status == CommandStatus.expired.value

    ack = client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "accepted"}, headers=headers)
    assert ack.status_code == 409
