"""Regresii pentru issue #10: claim atomic, ACK/rezultat idempotente cu
payload contradictoriu respins explicit, persistenta expirarii chiar cand
raspunsul respinge operatia, si dispatch concurent fara comenzi echivalente."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.security import utcnow
from app.models.command import Command, CommandEvent
from app.models.enums import CommandStatus, CommandType
from app.services import device_service
from tests.energy_helpers import command_for, ops_fixture  # noqa: F401
from tests.factories import make_device, make_org, make_station, make_user


def _claim(client, db, station, user):
    claim, raw_code = device_service.create_claim_code(db, station, user)
    db.commit()
    resp = client.post("/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}})
    payload = resp.json()
    return payload, {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}


def test_ack_replay_is_idempotent_but_contradiction_rejected(client, db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    cmd = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    headers = {"Authorization": f"Bearer {device.id}.ops-secret"}
    db.commit()

    client.get("/api/v1/commands/pending", headers=headers)  # marks delivered

    first = client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "accepted"}, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "accepted"

    # Reincercare identica (raspunsul original s-a pierdut in retea, de exemplu):
    # trebuie sa reuseasca la fel, nu sa fie o eroare doar pentru ca deja s-a intamplat.
    second = client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "accepted"}, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "accepted"

    different_reason = client.post(
        f"/api/v1/commands/{cmd.id}/ack",
        json={"status": "accepted", "reason": "different evidence"},
        headers=headers,
    )
    assert different_reason.status_code == 409

    # Un rezultat CONTRADICTORIU pentru aceeasi comanda deja finalizata e respins explicit.
    contradiction = client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "rejected"}, headers=headers)
    assert contradiction.status_code == 409

    events = db.scalars(select(CommandEvent).where(CommandEvent.command_id == cmd.id, CommandEvent.event_type == "accepted")).all()
    assert len(events) == 1, "reincercarea idempotenta nu trebuie sa duplice evenimentul de audit"


def test_result_replay_is_idempotent_but_contradiction_rejected(client, db, ops, monkeypatch):
    user, station, device, plan, interval, policy, now = ops
    cmd = command_for(db, ops, monkeypatch)
    monkeypatch.setattr(device_service, "utcnow", lambda: now + timedelta(minutes=1))
    headers = {"Authorization": f"Bearer {device.id}.ops-secret"}
    db.commit()

    client.get("/api/v1/commands/pending", headers=headers)
    client.post(f"/api/v1/commands/{cmd.id}/ack", json={"status": "accepted"}, headers=headers)

    details = {"applied": True, "observed_soc": 61.5}
    first = client.post(f"/api/v1/commands/{cmd.id}/result", json={"status": "executed", "details": details}, headers=headers)
    assert first.status_code == 200

    second = client.post(f"/api/v1/commands/{cmd.id}/result", json={"status": "executed", "details": details}, headers=headers)
    assert second.status_code == 200

    contradiction = client.post(
        f"/api/v1/commands/{cmd.id}/result",
        json={"status": "executed", "details": {"applied": True, "observed_soc": 40.0}},
        headers=headers,
    )
    assert contradiction.status_code == 409

    contradiction_status = client.post(f"/api/v1/commands/{cmd.id}/result", json={"status": "failed", "details": {}}, headers=headers)
    assert contradiction_status.status_code == 409

    events = db.scalars(select(CommandEvent).where(CommandEvent.command_id == cmd.id, CommandEvent.event_type == "executed")).all()
    assert len(events) == 1


def test_ack_expiry_persists_even_though_the_request_is_rejected(db):
    """Reproduce exact bug-ul semnalat: ack pe o comanda ramasa `created`/`delivered`
    dar deja expirata trebuie sa marcheze `expired` PERMANENT, chiar daca ruta
    API face `db.rollback()` dupa ce prinde DeviceServiceError (exact ce fac
    toate rutele din commands.py)."""
    user = make_user(db, email="ackexpiry@test.local", password="Password1234")
    org = make_org(db, "Ack Expiry Org")
    station = make_station(db, org, user, name="Ack Expiry Station")
    device = make_device(db, station)
    db.commit()

    cmd = Command(
        station_id=station.id, device_id=device.id,
        type=CommandType.hold_battery.value, parameters={},
        version=1, idempotency_key="ack-expiry-1", status=CommandStatus.delivered.value,
        author="user", reason="Test expiry persistence",
        valid_from=utcnow() - timedelta(minutes=20), expires_at=utcnow() - timedelta(minutes=1),
    )
    db.add(cmd)
    db.commit()

    with pytest.raises(device_service.CommandExpiredError, match="expirat"):
        device_service.acknowledge_command(db, device, cmd.id, "accepted", None)

    # Ruta detine limita tranzactiei si pastreaza numai tranzitia explicita.
    db.commit()

    db.refresh(cmd)
    assert cmd.status == CommandStatus.expired.value
    events = db.scalars(select(CommandEvent).where(CommandEvent.command_id == cmd.id)).all()
    assert any(e.event_type == "expired" for e in events)


def test_concurrent_claim_of_same_code_has_exactly_one_winner(engine):
    """Fixtura `db` (SAVEPOINT imbricat) nu poate exercita commit-uri reale
    concurente -- folosim `engine`-ul de test cu doua sesiuni/thread-uri reale,
    exact ca in testele de concurenta din test_inverter_config.py/test_optimization.py."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.models.device import ClaimCode, Device, DeviceCredential
    from app.models.organization import Organization
    from app.models.station import Station
    from app.models.user import User

    suffix = uuid.uuid4().hex
    with Session(engine) as setup:
        user = make_user(setup, email=f"{suffix}@claimrace.test")
        org = make_org(setup, f"Claim Race {suffix}")
        station = make_station(setup, org, user, name=f"Claim Race Station {suffix}")
        claim, raw_code = device_service.create_claim_code(setup, station, user)
        setup.commit()
        station_id, org_id, user_id, claim_id = station.id, org.id, user.id, claim.id

    barrier = Barrier(2)

    def attempt():
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                device_service.claim_device(session, raw_code, "Racing Device", {})
                session.commit()
                return "claimed"
            except device_service.DeviceServiceError:
                session.rollback()
                return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            outcomes = sorted(f.result(timeout=10) for f in futures)
        assert outcomes == ["claimed", "rejected"], "exact un castigator, celalalt respins curat"

        with Session(engine) as verify:
            devices = verify.scalars(select(Device).where(Device.station_id == station_id)).all()
            assert len(devices) == 1, "codul de asociere nu trebuie sa produca doua device-uri"
            credentials = verify.scalars(select(DeviceCredential).where(DeviceCredential.device_id == devices[0].id)).all()
            assert len(credentials) == 1
    finally:
        with Session(engine) as cleanup:
            # ClaimCode.claimed_device_id refera Device -- trebuie sters INAINTE
            # de device-ul castigator, altfel constrangerea FK respinge stergerea.
            cleanup.execute(delete(ClaimCode).where(ClaimCode.id == claim_id))
            device_ids = [d.id for d in cleanup.scalars(select(Device).where(Device.station_id == station_id)).all()]
            for did in device_ids:
                cleanup.execute(delete(DeviceCredential).where(DeviceCredential.device_id == did))
            cleanup.execute(delete(Device).where(Device.station_id == station_id))
            from app.models.audit import AuditLog
            cleanup.execute(delete(AuditLog).where(AuditLog.station_id == station_id))
            cleanup.execute(delete(Station).where(Station.id == station_id))
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_concurrent_dispatch_produces_no_duplicate_commands(engine, monkeypatch):
    """Doua rulari concurente ale `dispatch_due_commands` pentru aceeasi statie
    live/interval curent trebuie sa produca exact O comanda, niciodata doua
    echivalente, si niciuna dintre rulari nu trebuie sa se pravaleasca in bloc
    (coliziunea trebuie izolata, nu sa anuleze intreaga tranzactie/lot)."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.models.organization import Organization
    from app.models.user import User
    from app.services import command_dispatch_service
    from tests.energy_helpers import approve, make_control_context
    with Session(engine) as setup:
        context = make_control_context(setup, monkeypatch)
        user, station, device, plan, interval, policy, now = context
        approve(setup, context)
        device_service.accept_plan(setup, device, plan.version)
        setup.commit()
        station_id, org_id, user_id = station.id, station.organization_id, user.id
    monkeypatch.setattr(command_dispatch_service, "utcnow", lambda: now + timedelta(minutes=1))

    barrier = Barrier(2)

    def attempt():
        with Session(engine) as session:
            barrier.wait(timeout=5)
            created = command_dispatch_service.dispatch_due_commands(session)
            session.commit()
            return len(created)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            counts = [f.result(timeout=10) for f in futures]
        assert sum(counts) == 1, "exact o comanda trebuie creata in total, indiferent care rulare a castigat cursa"

        with Session(engine) as verify:
            commands = verify.scalars(select(Command).where(Command.station_id == station_id)).all()
            assert len(commands) == 1
    finally:
        with Session(engine) as cleanup:
            from app.models.audit import AuditLog
            from app.models.station import Station
            cleanup.execute(delete(AuditLog).where(AuditLog.station_id == station_id))
            cleanup.execute(delete(Station).where(Station.id == station_id))
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
