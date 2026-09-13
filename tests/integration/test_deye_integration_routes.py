"""Teste HTTP pentru fluxul de conectare/deconectare Deye Cloud (issue #43):
RBAC (doar organization_admin+ poate conecta/deconecta, viewer poate doar
vedea starea), izolare intre organizatii si ca niciun apel de retea real nu
are loc (autentificarea/listarea statiilor sunt monkeypatch-uite la nivelul
functiilor de serviciu, ca in restul suitei -- vezi `test_opcom_import.py`)."""
from __future__ import annotations

from app.core.rate_limit import reset_key
from app.models.deye_integration import DeyeCloudConnection
from app.models.enums import DeyeCloudConnectionStatus
from app.services import deye_cloud_service as svc
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _stub_auth(monkeypatch, stations=None):
    monkeypatch.setattr(svc, "authenticate", lambda email, password: {"access_token": "tok", "expires_in": 3600})
    monkeypatch.setattr(svc, "list_remote_stations", lambda token: stations or [{"id": 322, "name": "Casa Test"}])
    monkeypatch.setattr(svc, "list_remote_station_devices", lambda token, remote_station_id: [])


def test_viewer_can_see_status_page_but_not_connect_form(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeviewer@test.local", password="Password1234")
    org = make_org(db, "Deye Org Viewer")
    station = make_station(db, org, user, name="Statie Deye Viewer")
    make_membership(db, user, org, role="viewer")
    db.commit()

    login(client, "deyeviewer@test.local", "Password1234")
    resp = client.get(f"/stations/{station.id}/integrations/deye")
    assert resp.status_code == 200
    assert b"Doar un administrator de organizatie" in resp.content


def test_viewer_cannot_post_connect(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeviewer2@test.local", password="Password1234")
    org = make_org(db, "Deye Org Viewer2")
    station = make_station(db, org, user, name="Statie Deye Viewer2")
    make_membership(db, user, org, role="viewer")
    db.commit()

    login(client, "deyeviewer2@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "email": "a@b.com", "password": "x", "consent": "yes"},
    )
    assert resp.status_code == 403


def test_organization_admin_can_connect_select_and_disconnect(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeadmin@test.local", password="Password1234")
    org = make_org(db, "Deye Org Admin")
    station = make_station(db, org, user, name="Statie Deye Admin")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    _stub_auth(monkeypatch)

    login(client, "deyeadmin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    connect_resp = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "email": "client@example.com", "password": "hunter2", "consent": "yes"},
        follow_redirects=False,
    )
    assert connect_resp.status_code == 303
    assert "error" not in connect_resp.headers["location"]

    conn = db.query(DeyeCloudConnection).filter_by(station_id=station.id).one()
    assert conn.status == DeyeCloudConnectionStatus.pending_selection.value
    assert conn.account_email == "client@example.com"

    select_resp = client.post(
        f"/stations/{station.id}/integrations/deye/select",
        data={"csrf_token": csrf, "remote_station_id": 322, "remote_station_name": "Casa Test"},
        follow_redirects=False,
    )
    assert select_resp.status_code == 303
    db.refresh(conn)
    assert conn.status == DeyeCloudConnectionStatus.connected.value
    assert conn.remote_station_id == 322
    assert conn.device_id is not None

    page_resp = client.get(f"/stations/{station.id}/integrations/deye")
    assert page_resp.status_code == 200
    assert b"Casa Test" in page_resp.content

    disconnect_resp = client.post(
        f"/stations/{station.id}/integrations/deye/disconnect",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert disconnect_resp.status_code == 303
    db.refresh(conn)
    assert conn.status == DeyeCloudConnectionStatus.disconnected.value
    assert conn.encrypted_access_token is None


def test_connect_without_consent_is_rejected(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyenoconsent@test.local", password="Password1234")
    org = make_org(db, "Deye Org NoConsent")
    station = make_station(db, org, user, name="Statie Deye NoConsent")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    _stub_auth(monkeypatch)

    login(client, "deyenoconsent@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "email": "client@example.com", "password": "hunter2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "consimtamant" in resp.headers["location"]
    assert db.query(DeyeCloudConnection).filter_by(station_id=station.id).count() == 0


def test_connect_auth_failure_shows_generic_error_not_stack_trace(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeautherr@test.local", password="Password1234")
    org = make_org(db, "Deye Org AuthErr")
    station = make_station(db, org, user, name="Statie Deye AuthErr")
    make_membership(db, user, org, role="organization_admin")
    db.commit()

    def _raise(email, password):
        raise svc.DeyeCloudAuthError("parola gresita")

    monkeypatch.setattr(svc, "authenticate", _raise)

    login(client, "deyeautherr@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "email": "client@example.com", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "consimtamant" in resp.headers["location"]  # respins la fel ca lipsa consimtamant (nu s-a autentificat)


def test_other_org_admin_cannot_manage_a_foreign_stations_connection(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    owner = make_user(db, email="deyeowner@test.local", password="Password1234")
    intruder = make_user(db, email="deyeintruder@test.local", password="Password1234")
    org1 = make_org(db, "Deye Org Owner")
    org2 = make_org(db, "Deye Org Intruder")
    station = make_station(db, org1, owner, name="Statie Deye Owner")
    make_membership(db, owner, org1, role="organization_admin")
    make_membership(db, intruder, org2, role="organization_admin")
    db.commit()
    _stub_auth(monkeypatch)

    login(client, "deyeintruder@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "email": "client@example.com", "password": "hunter2", "consent": "yes"},
    )
    assert resp.status_code == 403
    assert db.query(DeyeCloudConnection).filter_by(station_id=station.id).count() == 0
