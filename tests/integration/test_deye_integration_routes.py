"""Teste HTTP pentru fluxul de conectare/deconectare Deye Cloud (issue #43):
RBAC (doar organization_admin+ poate conecta/deconecta, viewer poate doar
vedea starea), izolare intre organizatii si ca niciun apel de retea real nu
are loc (autentificarea/listarea statiilor sunt monkeypatch-uite la nivelul
functiilor de serviciu, ca in restul suitei -- vezi `test_opcom_import.py`)."""
from __future__ import annotations

from datetime import UTC, datetime

from app.core.rate_limit import RateLimitExceeded, reset_key
from app.models.deye_integration import DeyeCloudConnection
from app.models.enums import DeyeCloudConnectionStatus
from app.services import deye_cloud_service as svc
from app.web.routes import deye_integration as deye_routes
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _stub_auth(monkeypatch, stations=None):
    monkeypatch.setattr(svc, "authenticate", lambda app_id, app_secret, email, password: {"access_token": "tok", "expires_in": 3600})
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


def test_single_station_dashboard_links_to_deye_cloud_configuration(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyelink@test.local", password="Password1234")
    org = make_org(db, "Deye Org Link")
    station = make_station(db, org, user, name="Statie Deye Link")
    make_membership(db, user, org, role="organization_admin")
    db.commit()

    login(client, "deyelink@test.local", "Password1234")
    page = client.get("/", follow_redirects=True)

    assert page.status_code == 200
    assert f"/stations/{station.id}/integrations/deye" in page.text
    assert "Deye Cloud" in page.text


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
        data={"csrf_token": csrf, "app_id": "app", "app_secret": "secret", "email": "a@b.com", "password": "x", "consent": "yes"},
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
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "hunter2", "consent": "yes"},
        follow_redirects=False,
    )
    assert connect_resp.status_code == 303
    assert "error" not in connect_resp.headers["location"]

    conn = db.query(DeyeCloudConnection).filter_by(station_id=station.id).one()
    assert conn.status == DeyeCloudConnectionStatus.pending_selection.value
    assert conn.app_id == "station-app"
    assert conn.encrypted_app_secret != "station-secret"
    assert conn.account_email == "client@example.com"

    def _network_must_not_run(*args, **kwargs):
        raise AssertionError("GET-ul paginii nu trebuie sa apeleze Deye Cloud")

    monkeypatch.setattr(svc, "list_remote_stations", _network_must_not_run)
    pending_page = client.get(f"/stations/{station.id}/integrations/deye")
    assert pending_page.status_code == 200
    assert b"Casa Test" in pending_page.content

    select_resp = client.post(
        f"/stations/{station.id}/integrations/deye/select",
        data={"csrf_token": csrf, "remote_station_id": 322, "remote_station_name": "Nume fals"},
        follow_redirects=False,
    )
    assert select_resp.status_code == 303
    db.refresh(conn)
    assert conn.status == DeyeCloudConnectionStatus.connected.value
    assert conn.remote_station_id == 322
    assert conn.remote_station_name == "Casa Test"  # numele trimis de browser nu este folosit
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


def test_organization_admin_can_trigger_deye_history_import(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyehistory@test.local", password="Password1234")
    org = make_org(db, "Deye Org History")
    station = make_station(db, org, user, name="Statie Deye History")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    _stub_auth(monkeypatch)

    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "hunter2", "consent": "yes"},
    )
    client.post(
        f"/stations/{station.id}/integrations/deye/select",
        data={"csrf_token": csrf, "remote_station_id": 322},
    )
    db.expire_all()
    page = client.get(f"/stations/{station.id}/integrations/deye")
    assert page.status_code == 200
    assert b"Import istoric" in page.content

    seen = {}

    def _import(db_arg, connection, start, end):
        seen["connection_id"] = connection.id
        seen["start"] = start
        seen["end"] = end
        return {
            "created": 4,
            "skipped_existing": 2,
            "skipped_invalid_timestamp": 0,
            "implausible_power_rows": 0,
        }

    monkeypatch.setattr(svc, "import_station_history", _import)
    response = client.post(
        f"/stations/{station.id}/integrations/deye/import-history",
        data={"csrf_token": csrf, "start_at": "2026-01-01T10:00", "end_at": "2026-01-01T11:00"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Import+istoric+finalizat" in response.headers["location"]
    assert seen["start"] == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert seen["end"] == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def test_station_selection_rejects_forged_remote_id(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeforged@test.local", password="Password1234")
    org = make_org(db, "Deye Org Forged")
    station = make_station(db, org, user, name="Statie Deye Forged")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    _stub_auth(monkeypatch, stations=[{"id": 322, "name": "Casa Test"}])
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")
    client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "hunter2", "consent": "yes"},
    )

    response = client.post(
        f"/stations/{station.id}/integrations/deye/select",
        data={"csrf_token": csrf, "remote_station_id": 999},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    connection = db.query(DeyeCloudConnection).filter_by(station_id=station.id).one()
    assert connection.status == DeyeCloudConnectionStatus.pending_selection.value
    assert connection.device_id is None


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
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "hunter2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "consimtamant" in resp.headers["location"]
    assert db.query(DeyeCloudConnection).filter_by(station_id=station.id).count() == 0


def test_connect_rate_limit_blocks_provider_call(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeratelimit@test.local", password="Password1234")
    org = make_org(db, "Deye Org Rate Limit")
    station = make_station(db, org, user, name="Statie Deye Rate Limit")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    monkeypatch.setattr(
        deye_routes,
        "check_fixed_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(RateLimitExceeded(60)),
    )
    monkeypatch.setattr(
        svc,
        "authenticate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("providerul nu trebuie apelat")),
    )

    response = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "hunter2", "consent": "yes"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Prea+multe" in response.headers["location"]


def test_connect_auth_failure_shows_generic_error_not_stack_trace(client, db, monkeypatch):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="deyeautherr@test.local", password="Password1234")
    org = make_org(db, "Deye Org AuthErr")
    station = make_station(db, org, user, name="Statie Deye AuthErr")
    make_membership(db, user, org, role="organization_admin")
    db.commit()

    def _raise(app_id, app_secret, email, password):
        raise svc.DeyeCloudAuthError("parola gresita")

    monkeypatch.setattr(svc, "authenticate", _raise)

    login(client, "deyeautherr@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    resp = client.post(
        f"/stations/{station.id}/integrations/deye/connect",
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "wrong", "consent": "yes"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Autentificare" in resp.headers["location"]


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
        data={"csrf_token": csrf, "app_id": "station-app", "app_secret": "station-secret", "email": "client@example.com", "password": "hunter2", "consent": "yes"},
    )
    assert resp.status_code == 403
    assert db.query(DeyeCloudConnection).filter_by(station_id=station.id).count() == 0
