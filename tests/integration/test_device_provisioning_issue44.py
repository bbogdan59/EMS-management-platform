"""Teste pentru partea ramasa din issue #44 (dupa PR #59): transfer/factory
reset de device, dezactivarea explicita a fluxului legacy cu cod de asociere,
si un test de concurenta reala cu doua sesiuni Postgres separate pentru
revendicarea aceluiasi cod/device."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.rate_limit import reset_key
from app.core.security import verify_password
from app.models.audit import AuditLog
from app.models.device import ClaimCode, Device, DeviceCredential
from app.models.enums import DeviceStatus
from app.services import device_service
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _uuid() -> str:
    return str(uuid.uuid4())


def _allocated_device(db, station, admin) -> tuple[Device, str]:
    """Device complet alocat (installation_uuid + credentiala activa),
    exact ca la finalul fluxului real de enrollment automat."""
    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, f"secret-{installation_uuid}", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    raw_secret = device_service.allocate_device(db, device, station, admin)
    db.commit()
    return device, raw_secret


# --- Transfer (service-level) ---------------------------------------------


def test_transfer_moves_device_and_revokes_old_credential(db):
    admin = make_user(db, email="transfer1@test.local", is_platform_admin=True)
    org = make_org(db, "Transfer Org 1")
    station_a = make_station(db, org, admin, name="Transfer Station A1")
    station_b = make_station(db, org, admin, name="Transfer Station B1")
    db.commit()

    device, old_secret = _allocated_device(db, station_a, admin)
    old_credential = db.scalar(select(DeviceCredential).where(DeviceCredential.device_id == device.id))
    assert old_credential.is_active is True

    old_station_id, new_secret = device_service.transfer_device(db, device, station_b, admin)
    db.commit()

    assert old_station_id == station_a.id
    assert device.station_id == station_b.id
    assert device.status == DeviceStatus.active.value
    assert new_secret != old_secret

    db.refresh(old_credential)
    assert old_credential.is_active is False
    assert old_credential.revoked_at is not None

    credentials = db.scalars(select(DeviceCredential).where(DeviceCredential.device_id == device.id)).all()
    active = [c for c in credentials if c.is_active]
    assert len(active) == 1
    assert verify_password(new_secret, active[0].secret_hash)
    assert not verify_password(old_secret, active[0].secret_hash)


def test_transfer_rejects_same_station(db):
    admin = make_user(db, email="transfer2@test.local", is_platform_admin=True)
    org = make_org(db, "Transfer Org 2")
    station = make_station(db, org, admin, name="Transfer Station 2")
    db.commit()
    device, _secret = _allocated_device(db, station, admin)

    with pytest.raises(device_service.DeviceServiceError):
        device_service.transfer_device(db, device, station, admin)


def test_transfer_rejects_unassigned_device(db):
    admin = make_user(db, email="transfer3@test.local", is_platform_admin=True)
    org = make_org(db, "Transfer Org 3")
    station = make_station(db, org, admin, name="Transfer Station 3")
    db.commit()

    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-unassigned-01", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))

    with pytest.raises(device_service.DeviceServiceError):
        device_service.transfer_device(db, device, station, admin)


def test_transfer_allows_cross_organization_move(db):
    """Decizie deliberata (documentata in docs/LIMITATIONS.md): transferul e
    o actiune strict platform_admin si POATE muta un device intre organizatii
    diferite -- scenariul real de hardware revandut/reinstalat."""
    admin = make_user(db, email="transfer4@test.local", is_platform_admin=True)
    org_a = make_org(db, "Transfer Org 4a")
    org_b = make_org(db, "Transfer Org 4b")
    station_a = make_station(db, org_a, admin, name="Transfer Station 4a")
    station_b = make_station(db, org_b, admin, name="Transfer Station 4b")
    db.commit()

    device, _secret = _allocated_device(db, station_a, admin)
    device_service.transfer_device(db, device, station_b, admin)
    db.commit()

    assert device.station_id == station_b.id


# --- Factory reset (service-level) ----------------------------------------


def test_factory_reset_detaches_and_revokes(db):
    admin = make_user(db, email="reset1@test.local", is_platform_admin=True)
    org = make_org(db, "Reset Org 1")
    station = make_station(db, org, admin, name="Reset Station 1")
    db.commit()
    device, raw_secret = _allocated_device(db, station, admin)

    device_service.factory_reset_device(db, device)
    db.commit()

    assert device.station_id is None
    assert device.status == DeviceStatus.pending_claim.value
    assert device.pending_credential_secret is None
    assert device.allocated_at is None
    assert device.allocated_by_user_id is None
    # identitatea proprie a dispozitivului nu e atinsa de un reset administrativ
    assert device.installation_uuid is not None
    assert device.provisioning_secret_hash is not None

    credentials = db.scalars(select(DeviceCredential).where(DeviceCredential.device_id == device.id)).all()
    assert all(not c.is_active for c in credentials)
    assert not any(verify_password(raw_secret, c.secret_hash) and c.is_active for c in credentials)

    # Device-ul poate fi realocat, exact ca un enrollment neexpirat normal.
    other_station = make_station(db, org, admin, name="Reset Station 1b")
    db.commit()
    new_secret = device_service.allocate_device(db, device, other_station, admin)
    db.commit()
    assert device.station_id == other_station.id
    assert new_secret is not None


def test_factory_reset_rejects_unassigned_device(db):
    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-reset-unassigned", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))

    with pytest.raises(device_service.DeviceServiceError):
        device_service.factory_reset_device(db, device)


# --- Transfer / factory reset HTTP + RBAC/CSRF/audit -----------------------


def test_http_transfer_requires_platform_admin(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="notplatform1@test.local", password="Password1234")
    org = make_org(db, "Not Platform Org 1")
    make_membership(db, user, org, role="organization_admin")
    station_a = make_station(db, org, user, name="Not Platform Station A")
    station_b = make_station(db, org, user, name="Not Platform Station B")
    db.commit()
    device, _secret = _allocated_device(db, station_a, user)

    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/admin/devices/{device.id}/transfer",
        data={"csrf_token": csrf, "target_station_id": str(station_b.id), "confirm_identifier": device.serial_number or device.installation_uuid},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 403)
    db.expire_all()
    assert db.get(Device, device.id).station_id == station_a.id


def test_http_transfer_requires_exact_confirmation(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="transferhttp1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Transfer HTTP Org 1")
    station_a = make_station(db, org, admin, name="Transfer HTTP Station A1")
    station_b = make_station(db, org, admin, name="Transfer HTTP Station B1")
    db.commit()
    device, _secret = _allocated_device(db, station_a, admin)

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/admin/devices/{device.id}/transfer",
        data={"csrf_token": csrf, "target_station_id": str(station_b.id), "confirm_identifier": "wrong-value"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?error=confirm_mismatch")
    db.expire_all()
    assert db.get(Device, device.id).station_id == station_a.id


def test_http_transfer_full_flow_shows_new_credential(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="transferhttp2@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Transfer HTTP Org 2")
    station_a = make_station(db, org, admin, name="Transfer HTTP Station A2")
    station_b = make_station(db, org, admin, name="Transfer HTTP Station B2")
    db.commit()
    device, old_secret = _allocated_device(db, station_a, admin)
    confirm_value = device.installation_uuid

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/admin/devices/{device.id}/transfer",
        data={"csrf_token": csrf, "target_station_id": str(station_b.id), "confirm_identifier": confirm_value},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "credential" in resp.text.lower() or str(device.id) in resp.text

    db.expire_all()
    device = db.get(Device, device.id)
    assert device.station_id == station_b.id

    audit_actions = {a for (a,) in db.execute(select(AuditLog.action))}
    assert "device_transferred" in audit_actions


def test_http_factory_reset_full_flow(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="resethttp1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Reset HTTP Org 1")
    station = make_station(db, org, admin, name="Reset HTTP Station 1")
    db.commit()
    device, _secret = _allocated_device(db, station, admin)
    confirm_value = device.installation_uuid

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/admin/devices/{device.id}/factory-reset",
        data={"csrf_token": csrf, "confirm_identifier": confirm_value},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?reset=1")

    db.expire_all()
    device = db.get(Device, device.id)
    assert device.station_id is None
    assert device.status == DeviceStatus.pending_claim.value


# --- Fluxul legacy dezactivat -----------------------------------------------


def test_production_rejects_legacy_claim_code_enabled():
    from app.config import Settings

    with pytest.raises(RuntimeError, match="legacy"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key="test-only-explicit-key",
            demo_mode_enabled=False,
            opcom_use_synthetic_fixture_on_failure=False,
            session_cookie_secure=True,
            email_backend="smtp",
            legacy_claim_code_enabled=True,
        )


def test_legacy_claim_code_is_disabled_by_default():
    from app.config import Settings

    assert Settings.model_fields["legacy_claim_code_enabled"].default is False


def test_web_legacy_claim_code_route_disabled_returns_error(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="legacyweb1@test.local", password="Password1234")
    org = make_org(db, "Legacy Web Org 1")
    make_membership(db, user, org, role="organization_admin")
    station = make_station(db, org, user, name="Legacy Web Station 1")
    db.commit()

    monkeypatch.setattr(get_settings(), "legacy_claim_code_enabled", False)

    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/claim-codes",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?error=legacy_disabled")

    claim_codes = db.scalars(select(ClaimCode).where(ClaimCode.station_id == station.id)).all()
    assert claim_codes == []


def test_api_legacy_claim_endpoint_disabled_returns_410(client, db, monkeypatch):
    admin = make_user(db, email="legacyapi1@test.local", is_platform_admin=True)
    org = make_org(db, "Legacy Api Org 1")
    station = make_station(db, org, admin, name="Legacy Api Station 1")
    db.commit()
    claim, raw_code = device_service.create_claim_code(db, station, admin)
    db.commit()

    monkeypatch.setattr(get_settings(), "legacy_claim_code_enabled", False)

    resp = client.post(
        "/api/v1/devices/claim", json={"claim_code": raw_code, "device_name": "D", "hardware_info": {}}
    )
    assert resp.status_code == 410

    # codul ramane neconsumat -- refuzul e la nivel de flag, nu invalideaza codul
    db.expire_all()
    claim = db.get(ClaimCode, claim.id)
    assert claim.status == "pending"


# --- Randare template: wizard-ul de onboarding si pagina de admin ---------


def test_station_creation_redirects_into_device_claim_wizard_step(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="wizard1@test.local", password="Password1234")
    org = make_org(db, "Wizard Org 1")
    make_membership(db, user, org, role="organization_admin")
    db.commit()

    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": csrf, "name": "Wizard Station", "timezone": "Europe/Bucharest",
            "latitude": "44.43", "longitude": "26.10", "pv_installed_power_kw": "5", "inverter_power_kw": "5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/devices?onboarding=1" in resp.headers["location"]

    followed = client.get(resp.headers["location"])
    assert followed.status_code == 200
    assert "Pasul 2 din 3" in followed.text
    assert "Device Code" in followed.text


def test_onboarding_activation_success_links_to_config_step(client, db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Wizard Org 2")
    customer = make_user(db, email="wizard2@test.local", password="Password1234")
    make_membership(db, customer, org, role="organization_admin")
    station = make_station(db, org, customer, name="Wizard Station 2")
    code = "ACT-WIZAR-DABCD-EFGHI-JKLMN-O"
    device_service.enroll_device(
        db, _uuid(), "provisioning-secret-wizard-01", {},
        serial_number="EMS-WIZA-RD00-0001", activation_code=code,
    )
    db.commit()

    login(client, customer.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/devices/activate",
        data={"csrf_token": csrf, "activation_code": code, "onboarding": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?linked=1&onboarding=1")

    followed = client.get(resp.headers["location"])
    assert followed.status_code == 200
    assert f"/stations/{station.id}/config" in followed.text


def test_admin_assigned_devices_page_renders_with_online_device(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="assignedpage1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Assigned Page Org 1")
    station = make_station(db, org, admin, name="Assigned Page Station 1")
    db.commit()
    device, _secret = _allocated_device(db, station, admin)
    device_service.record_heartbeat(db, device, "boot-1", "1.0", {})
    db.commit()

    login(client, admin.email, "Password1234")
    resp = client.get("/admin/devices/assigned")
    assert resp.status_code == 200
    assert "online" in resp.text
    assert (device.serial_number or device.installation_uuid) in resp.text


# --- Flota completa: dashboard admin cross-statie (online/offline, linked/unlinked, stats) --


def test_record_heartbeat_stores_system_stats(db):
    admin = make_user(db, email="fleetstats1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Fleet Stats Org 1")
    station = make_station(db, org, admin, name="Fleet Stats Station 1")
    db.commit()
    device, _secret = _allocated_device(db, station, admin)

    device_service.record_heartbeat(
        db, device, "boot-1", "1.0", {},
        {"cpu_load_1m": 0.42, "memory_used_percent": 51, "temperature_c": 46.5},
    )
    db.commit()
    db.refresh(device)
    assert device.system_stats == {"cpu_load_1m": 0.42, "memory_used_percent": 51, "temperature_c": 46.5}


def test_record_heartbeat_replaces_stale_system_stats(db):
    """A stat the device stops reporting (e.g. a sensor read failure) must
    disappear on the next heartbeat, not linger as stale data -- unlike
    `capabilities`, which IS merged."""
    admin = make_user(db, email="fleetstats2@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Fleet Stats Org 2")
    station = make_station(db, org, admin, name="Fleet Stats Station 2")
    db.commit()
    device, _secret = _allocated_device(db, station, admin)

    device_service.record_heartbeat(db, device, "boot-1", "1.0", {}, {"temperature_c": 50.0, "cpu_load_1m": 1.1})
    db.commit()
    device_service.record_heartbeat(db, device, "boot-1", "1.0", {}, {"cpu_load_1m": 0.2})
    db.commit()
    db.refresh(device)
    assert device.system_stats == {"cpu_load_1m": 0.2}


def test_admin_fleet_devices_page_shows_online_offline_linked_and_stats(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="fleetpage1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Fleet Page Org 1")
    station = make_station(db, org, admin, name="Fleet Page Station 1")
    db.commit()

    linked_device, _secret = _allocated_device(db, station, admin)
    device_service.record_heartbeat(
        db, linked_device, "boot-1", "1.2.3", {}, {"cpu_load_1m": 0.5, "temperature_c": 47.0}
    )
    db.commit()

    unlinked_uuid = _uuid()
    device_service.enroll_device(db, unlinked_uuid, f"secret-{unlinked_uuid}", {})
    db.commit()

    login(client, admin.email, "Password1234")
    resp = client.get("/admin/devices")
    assert resp.status_code == 200
    assert "online" in resp.text
    assert "offline" in resp.text
    assert "linked" in resp.text
    assert "unlinked" in resp.text
    assert "1.2.3" in resp.text
    assert "47.0" in resp.text
    assert (linked_device.serial_number or linked_device.installation_uuid) in resp.text
    assert unlinked_uuid in resp.text


def test_admin_fleet_devices_page_requires_platform_admin(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="fleetnotadmin1@test.local", password="Password1234")
    org = make_org(db, "Fleet Not Admin Org 1")
    make_station(db, org, user, name="Fleet Not Admin Station 1")
    db.commit()

    login(client, "fleetnotadmin1@test.local", "Password1234")
    resp = client.get("/admin/devices", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


# --- Concurenta reala: doua sesiuni Postgres pentru acelasi cod/device -----


def test_concurrent_claim_two_accounts_same_code_exactly_one_wins(engine):
    """Doua conturi/sesiuni distincte incearca sa revendice ACELASI cod de
    asociere in acelasi timp -- exact scenariul cerut de issue #44 ("doua
    conturi care revendica simultan"). Foloseste `engine` (conexiuni Postgres
    reale, commit-uri reale), NU fixtura `db` cu SAVEPOINT, care nu poate
    exercita contentie reala de lock -- acelasi tipar ca
    `tests/integration/test_device_protocol_hardening.py::
    test_concurrent_claim_of_same_code_has_exactly_one_winner` si
    `tests/integration/test_device_enrollment.py::
    test_enroll_concurrent_same_identity_creates_exactly_one_device`."""
    from sqlalchemy import delete

    suffix = uuid.uuid4().hex
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with Session(engine) as setup:
        admin = make_user(setup, email=f"race-admin-{suffix}@test.local")
        org_a = make_org(setup, f"Race Org A {suffix}")
        org_b = make_org(setup, f"Race Org B {suffix}")
        account_a = make_user(setup, email=f"race-a-{suffix}@test.local")
        account_b = make_user(setup, email=f"race-b-{suffix}@test.local")
        make_membership(setup, account_a, org_a, role="organization_admin")
        make_membership(setup, account_b, org_b, role="organization_admin")
        station_a = make_station(setup, org_a, admin, name=f"Race Station A {suffix}")
        station_b = make_station(setup, org_b, admin, name=f"Race Station B {suffix}")
        claim, raw_code = device_service.create_claim_code(setup, station_a, admin)
        setup.commit()
        station_a_id, station_b_id, claim_id = station_a.id, station_b.id, claim.id

    barrier = Barrier(2)

    def attempt(device_name: str):
        with SessionFactory() as session:
            barrier.wait(timeout=5)
            try:
                device, _secret = device_service.claim_device(session, raw_code, device_name, {})
                session.commit()
                return ("claimed", device.id)
            except device_service.DeviceServiceError:
                session.rollback()
                return ("rejected", None)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(attempt, "Account A Device")
            future_b = pool.submit(attempt, "Account B Device")
            outcome_a = future_a.result(timeout=10)
            outcome_b = future_b.result(timeout=10)

        outcomes = sorted([outcome_a[0], outcome_b[0]])
        assert outcomes == ["claimed", "rejected"], "exact un cont trebuie sa castige, celalalt respins curat, fara deadlock"

        winner_device_id = outcome_a[1] or outcome_b[1]
        assert winner_device_id is not None

        with SessionFactory() as verify:
            devices_a = verify.scalars(select(Device).where(Device.station_id == station_a_id)).all()
            devices_b = verify.scalars(select(Device).where(Device.station_id == station_b_id)).all()
            # Codul era emis pentru station_a -- indiferent care cont/thread a
            # castigat cursa, device-ul rezultat apartine statiei pentru care
            # a fost emis codul; NICIODATA doua device-uri (fara dublare) si
            # niciodata pe cealalta statie (fara cross-tenant silentios).
            assert len(devices_a) == 1
            assert devices_a[0].id == winner_device_id
            assert devices_b == []

            claim_row = verify.get(ClaimCode, claim_id)
            assert claim_row.status == "claimed"
            assert claim_row.claimed_device_id == winner_device_id
    finally:
        with SessionFactory() as cleanup:
            cleanup.execute(delete(ClaimCode).where(ClaimCode.id == claim_id))
            device_ids = [
                d.id
                for d in cleanup.scalars(
                    select(Device).where(Device.station_id.in_([station_a_id, station_b_id]))
                ).all()
            ]
            for did in device_ids:
                cleanup.execute(delete(DeviceCredential).where(DeviceCredential.device_id == did))
                cleanup.execute(delete(Device).where(Device.id == did))
            cleanup.commit()
