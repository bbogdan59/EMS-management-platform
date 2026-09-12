from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.rate_limit import reset_key
from app.core.security import hash_token, utcnow, verify_password
from app.models.device import Device, DeviceCredential
from app.models.enums import DeviceStatus
from app.services import device_service
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _uuid() -> str:
    return str(uuid.uuid4())


def test_enroll_creates_pending_device_without_station(db):
    installation_uuid = _uuid()
    result = device_service.enroll_device(db, installation_uuid, "provisioning-secret-1234567890", {"model": "test"})
    db.commit()

    assert result["status"] == "pending"
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    assert device is not None
    assert device.station_id is None
    assert device.status == DeviceStatus.pending_claim.value
    assert device.provisioning_secret_hash is not None
    assert device.enrollment_expires_at is not None


def test_enroll_is_idempotent_replay(db):
    installation_uuid = _uuid()
    secret = "provisioning-secret-replay-0001"

    first = device_service.enroll_device(db, installation_uuid, secret, {})
    db.commit()
    second = device_service.enroll_device(db, installation_uuid, secret, {})
    db.commit()

    assert first["status"] == second["status"] == "pending"
    matching = db.scalars(select(Device).where(Device.installation_uuid == installation_uuid)).all()
    assert len(matching) == 1  # nicio dublare la reincercare


def test_enroll_rejects_spoofed_identity(db):
    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "real-secret-0000000000000000", {})
    db.commit()

    with pytest.raises(device_service.DeviceServiceError):
        device_service.enroll_device(db, installation_uuid, "wrong-secret-000000000000000", {})


def test_enroll_after_allocation_returns_assigned_with_credential(db):
    user = make_user(db, email="enroll1@test.local")
    org = make_org(db, "Enroll Org 1")
    station = make_station(db, org, user, name="Enroll Station 1")
    db.commit()

    installation_uuid = _uuid()
    secret = "provisioning-secret-allocated-01"
    device_service.enroll_device(db, installation_uuid, secret, {})
    db.commit()

    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    raw_credential = device_service.allocate_device(db, device, station, user)
    db.commit()

    result = device_service.enroll_device(db, installation_uuid, secret, {})
    assert result["status"] == "assigned"
    assert result["station_id"] == station.id
    assert result["credential_secret"] == raw_credential  # recuperabil idempotent pana la prima folosire


def test_credential_secret_cleared_after_first_authenticated_use(db):
    user = make_user(db, email="enroll2@test.local")
    org = make_org(db, "Enroll Org 2")
    station = make_station(db, org, user, name="Enroll Station 2")
    db.commit()

    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-clear-000001", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    device_service.allocate_device(db, device, station, user)
    db.commit()
    assert device.pending_credential_secret is not None

    device_service.mark_bootstrap_credential_delivered(db, device)
    db.commit()
    assert device.pending_credential_secret is None


def test_allocate_rejects_already_assigned_device(db):
    user = make_user(db, email="enroll3@test.local")
    org = make_org(db, "Enroll Org 3")
    station_a = make_station(db, org, user, name="Enroll Station 3a")
    station_b = make_station(db, org, user, name="Enroll Station 3b")
    db.commit()

    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-tenant-0001", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    device_service.allocate_device(db, device, station_a, user)
    db.commit()

    with pytest.raises(device_service.DeviceServiceError):
        device_service.allocate_device(db, device, station_b, user)  # nicio schimbare silentioasa de tenant


def test_allocate_rejects_expired_enrollment(db):
    user = make_user(db, email="enroll4@test.local")
    org = make_org(db, "Enroll Org 4")
    station = make_station(db, org, user, name="Enroll Station 4")
    db.commit()

    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-expired-0001", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    device.enrollment_expires_at = utcnow() - timedelta(hours=1)
    db.add(device)
    db.commit()

    with pytest.raises(device_service.DeviceServiceError):
        device_service.allocate_device(db, device, station, user)


def test_allocate_rejects_revoked_device(db):
    user = make_user(db, email="enroll5@test.local")
    org = make_org(db, "Enroll Org 5")
    station = make_station(db, org, user, name="Enroll Station 5")
    db.commit()

    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-revoked-0001", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    device_service.revoke_device(db, device, reason="test")
    db.commit()

    assert device_service.enroll_device(db, installation_uuid, "provisioning-secret-revoked-0001", {})["status"] == "revoked"
    with pytest.raises(device_service.DeviceServiceError):
        device_service.allocate_device(db, device, station, user)


def test_allocated_device_authenticates_with_new_credential(db):
    """Contractul de autentificare existent (Bearer device_id.secret) trebuie
    sa functioneze neschimbat pentru un device provenit din enrollment automat."""
    user = make_user(db, email="enroll6@test.local")
    org = make_org(db, "Enroll Org 6")
    station = make_station(db, org, user, name="Enroll Station 6")
    db.commit()

    installation_uuid = _uuid()
    device_service.enroll_device(db, installation_uuid, "provisioning-secret-auth-000001", {})
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    raw_secret = device_service.allocate_device(db, device, station, user)
    db.commit()

    credential = db.scalar(select(DeviceCredential).where(DeviceCredential.device_id == device.id))
    assert credential is not None
    assert credential.is_active is True
    assert verify_password(raw_secret, credential.secret_hash)


def test_enroll_concurrent_same_identity_creates_exactly_one_device(engine):
    """Cursa reala la nivel de baza de date (nu doar la nivel de proces):
    doua thread-uri, doua sesiuni SQLAlchemy separate, acelasi
    installation_uuid, trimise cat mai simultan posibil."""
    installation_uuid = _uuid()
    secret = "provisioning-secret-race-00000001"
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    results = []
    errors = []

    def _attempt():
        session = SessionFactory()
        try:
            r = device_service.enroll_device(session, installation_uuid, secret, {})
            session.commit()
            results.append(r)
        except Exception as exc:  # pragma: no cover - doar pentru diagnostic la esec
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: _attempt(), range(8)))

        assert not errors, f"Erori neasteptate: {errors}"
        assert len(results) == 8
        assert all(r["status"] == "pending" for r in results)

        verify_session = SessionFactory()
        try:
            matching = verify_session.scalars(select(Device).where(Device.installation_uuid == installation_uuid)).all()
            assert len(matching) == 1, "cursa concurenta a produs mai mult de un device pentru acelasi installation_uuid"
        finally:
            verify_session.close()
    finally:
        cleanup = SessionFactory()
        try:
            cleanup.execute(
                Device.__table__.delete().where(Device.__table__.c.installation_uuid == installation_uuid)
            )
            cleanup.commit()
        finally:
            cleanup.close()


# --- Contract HTTP end-to-end (POST /api/v1/devices/enroll + alocare admin) ---


def test_http_enroll_bootstrap_and_admin_allocation_full_flow(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="enrolladmin1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Enroll HTTP Org 1")
    station = make_station(db, org, admin, name="Enroll HTTP Station 1")
    db.commit()

    installation_uuid = _uuid()
    body = {"installation_uuid": installation_uuid, "provisioning_secret": "http-provisioning-secret-01", "hardware_info": {"model": "x"}}

    resp1 = client.post("/api/v1/devices/enroll", json=body)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "pending"

    # Reincercare idempotenta cu aceeasi identitate -- niciun al doilea device.
    resp2 = client.post("/api/v1/devices/enroll", json=body)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "pending"
    matching = db.scalars(select(Device).where(Device.installation_uuid == installation_uuid)).all()
    assert len(matching) == 1

    # Identitate falsificata: alt secret pentru acelasi installation_uuid.
    bad_body = {**body, "provisioning_secret": "cu-totul-alt-secret-0000000001"}
    resp_bad = client.post("/api/v1/devices/enroll", json=bad_body)
    assert resp_bad.status_code == 409

    device_id = matching[0].id

    login(client, "enrolladmin1@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    allocate_resp = client.post(
        f"/admin/devices/{device_id}/allocate",
        data={"csrf_token": csrf, "station_id": str(station.id)},
        follow_redirects=False,
    )
    assert allocate_resp.status_code == 200
    assert "credential" in allocate_resp.text.lower() or str(device_id) in allocate_resp.text

    # Device-ul poate acum recupera idempotent credentiala prin acelasi enroll.
    resp3 = client.post("/api/v1/devices/enroll", json=body)
    assert resp3.status_code == 200
    payload = resp3.json()
    assert payload["status"] == "assigned"
    assert payload["station_id"] == str(station.id)
    assert payload["credential_secret"] is not None

    auth_header = {"Authorization": f"Bearer {payload['device_id']}.{payload['credential_secret']}"}
    heartbeat_resp = client.post(
        "/api/v1/devices/heartbeat", json={"boot_id": "boot-enroll-1", "firmware_version": "0.1", "capabilities": {}}, headers=auth_header,
    )
    assert heartbeat_resp.status_code == 200

    db.expire_all()
    device = db.get(Device, device_id)
    assert device.pending_credential_secret is None  # sters dupa prima folosire reusita


def test_http_pending_devices_admin_ui_requires_platform_admin(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="notadmin1@test.local", password="Password1234")
    org = make_org(db, "Enroll HTTP Org 2")
    make_station(db, org, user, name="Enroll HTTP Station 2")
    db.commit()

    login(client, "notadmin1@test.local", "Password1234")
    resp = client.get("/admin/devices/pending", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


def test_new_enrollment_stores_public_serial_and_only_activation_hash(db):
    installation_uuid = _uuid()
    code = "ACT-ABCDE-FGHIJ-KLMNO-PQRST-UVWXY-Z"
    device_service.enroll_device(
        db,
        installation_uuid,
        "provisioning-secret-label-000001",
        {"platform": "raspberry-pi-4"},
        serial_number="EMS-ABCD-EFGH-IJKL-MNOP",
        activation_code=code,
    )
    db.commit()

    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    assert device.serial_number == "EMS-ABCD-EFGH-IJKL-MNOP"
    assert device.activation_code_hash == hash_token(code)
    assert code not in str(device.capabilities)


def test_valid_device_proof_renews_expired_pending_enrollment(db):
    installation_uuid = _uuid()
    secret = "provisioning-secret-renew-000001"
    kwargs = {
        "serial_number": "EMS-RENE-WABC-DEFG-HIJK",
        "activation_code": "ACT-RENEW-ABCDE-FGHIJ-KLMNO-P",
    }
    device_service.enroll_device(db, installation_uuid, secret, {}, **kwargs)
    db.commit()
    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    device.enrollment_expires_at = utcnow() - timedelta(days=20)
    db.commit()

    result = device_service.enroll_device(db, installation_uuid, secret, {}, **kwargs)
    db.commit()
    assert result["status"] == "pending"
    assert device.enrollment_expires_at > utcnow()


def test_customer_admin_claims_sealed_device_code_without_receiving_device_secret(client, db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Customer Device Claim")
    customer = make_user(db, email="customer-device@test.local", password="Password1234")
    make_membership(db, customer, org, role="organization_admin")
    station = make_station(db, org, customer, name="Customer Home")
    code = "ACT-CUSTO-MERDE-VICEC-ODE12-3"
    installation_uuid = _uuid()
    device_service.enroll_device(
        db, installation_uuid, "provisioning-secret-customer-01", {},
        serial_number="EMS-CUST-OMER-0001", activation_code=code,
    )
    db.commit()

    login(client, customer.email, "Password1234")
    response = client.post(
        f"/stations/{station.id}/devices/activate",
        data={"csrf_token": client.cookies.get("ems_csrf"), "activation_code": code.lower()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?linked=1")
    assert "credential" not in response.text.lower()

    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    assert device.station_id == station.id
    assert device.status == DeviceStatus.active.value
    assert device.activation_code_hash is None
    assert device.activation_claimed_at is not None
    assert device.pending_credential_secret is not None  # only the authenticated device can recover it


def test_device_code_is_one_use_and_failure_does_not_enumerate_inventory(client, db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "One Use Device Claim")
    customer = make_user(db, email="one-use-device@test.local", password="Password1234")
    make_membership(db, customer, org, role="organization_admin")
    station = make_station(db, org, customer, name="One Use Home")
    code = "ACT-ONEUS-EABCD-EFGHI-JKLMN-O"
    device_service.enroll_device(
        db, _uuid(), "provisioning-secret-one-use-01", {},
        serial_number="EMS-ONEU-SE00-0001", activation_code=code,
    )
    db.commit()
    login(client, customer.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    first = client.post(
        f"/stations/{station.id}/devices/activate",
        data={"csrf_token": csrf, "activation_code": code}, follow_redirects=False,
    )
    second = client.post(
        f"/stations/{station.id}/devices/activate",
        data={"csrf_token": csrf, "activation_code": code}, follow_redirects=False,
    )
    unknown = client.post(
        f"/stations/{station.id}/devices/activate",
        data={"csrf_token": csrf, "activation_code": "ACT-NOTTH-ERE00-00000-00000-0"}, follow_redirects=False,
    )
    assert first.headers["location"].endswith("?linked=1")
    assert second.headers["location"] == unknown.headers["location"]
    assert second.headers["location"].endswith("?error=invalid_device_code")
