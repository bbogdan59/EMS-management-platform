"""Teste HTTP pentru backoffice-ul de organizatii (issue #24): pagina de
detaliu admin, editare profil, suspendare/reactivare/arhivare/restaurare,
arhivare/restaurare statie, si efectul suspendarii/arhivarii asupra
accesului membrilor (verificat prin rutele existente `/organizations/*`,
`OrganizationAccess`/`StationAccess`)."""
from __future__ import annotations

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.audit import AuditLog
from app.models.organization import Organization
from app.models.station import Station
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import get_csrf, login


def _setup(db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="orgroutes-admin@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Org Routes Co")
    org_admin = make_user(db, email="orgroutes-orgadmin@test.local", password="Password1234")
    viewer = make_user(db, email="orgroutes-viewer@test.local", password="Password1234")
    make_membership(db, org_admin, org, role="organization_admin")
    make_membership(db, viewer, org, role="viewer")
    station = make_station(db, org, admin, name="Org Routes Station")
    db.commit()
    return admin, org, org_admin, viewer, station


def test_admin_organizations_detail_requires_platform_admin(client, db):
    admin, org, org_admin, viewer, station = _setup(db)

    login(client, org_admin.email, "Password1234")
    resp = client.get(f"/admin/organizations/{org.id}", follow_redirects=False)
    assert resp.status_code == 403

    reset_key("login_attempts:testclient")
    login(client, admin.email, "Password1234")
    resp = client.get(f"/admin/organizations/{org.id}")
    assert resp.status_code == 200
    assert "Org Routes Co" in resp.text


def test_suspend_reactivate_archive_restore_flow(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/admin/organizations/{org.id}/suspend",
        data={"csrf_token": csrf, "reason": "test suspend"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Organization, org.id).status == "suspended"

    resp = client.post(f"/admin/organizations/{org.id}/reactivate", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Organization, org.id).status == "active"

    resp = client.post(
        f"/admin/organizations/{org.id}/archive",
        data={"csrf_token": csrf, "reason": "offboarding"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Organization, org.id).status == "archived"

    resp = client.post(f"/admin/organizations/{org.id}/restore", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Organization, org.id).status == "active"

    entries = db.scalars(select(AuditLog).where(AuditLog.organization_id == org.id)).all()
    actions = {e.action for e in entries}
    assert {"organization_suspended", "organization_active", "organization_archived"} <= actions


def test_mutation_routes_require_csrf(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")

    resp = client.post(f"/admin/organizations/{org.id}/suspend", data={"reason": "no csrf"}, follow_redirects=False)
    assert resp.status_code == 403


def test_suspended_organization_blocks_write_but_not_read_for_members(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(f"/admin/organizations/{org.id}/suspend", data={"csrf_token": csrf, "reason": "billing"}, follow_redirects=False)

    reset_key("login_attempts:testclient")
    login(client, org_admin.email, "Password1234")
    org_csrf = get_csrf(client)

    # Scriere (creare statie, organization_admin+) trebuie blocata.
    resp = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": org_csrf, "name": "Blocked Station", "timezone": "Europe/Bucharest",
            "latitude": "44.4", "longitude": "26.1", "pv_installed_power_kw": "5", "inverter_power_kw": "5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403

    reset_key("login_attempts:testclient")
    login(client, viewer.email, "Password1234")
    # Citire (viewer) trebuie sa ramana permisa cat organizatia e doar suspendata.
    resp = client.get(f"/organizations/{org.id}")
    assert resp.status_code == 200


def test_archived_organization_blocks_all_member_access_including_read(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(f"/admin/organizations/{org.id}/archive", data={"csrf_token": csrf, "reason": "closed"}, follow_redirects=False)

    reset_key("login_attempts:testclient")
    login(client, viewer.email, "Password1234")
    resp = client.get(f"/organizations/{org.id}", follow_redirects=False)
    assert resp.status_code == 403

    # platform_admin ramane neafectat -- trebuie sa poata gestiona/restaura.
    reset_key("login_attempts:testclient")
    login(client, admin.email, "Password1234")
    resp = client.get(f"/admin/organizations/{org.id}")
    assert resp.status_code == 200


def test_station_archive_and_restore(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(f"/admin/stations/{station.id}/archive", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Station, station.id).is_active is False

    resp = client.post(f"/admin/stations/{station.id}/restore", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Station, station.id).is_active is True


def test_edit_organization_profile(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/admin/organizations/{org.id}/edit",
        data={"csrf_token": csrf, "name": "Renamed Co", "billing_email": "b@renamed.ro", "notes": "vip"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    updated = db.get(Organization, org.id)
    assert updated.name == "Renamed Co"
    assert updated.billing_email == "b@renamed.ro"
    assert updated.notes == "vip"


def test_suspend_without_reason_is_rejected(client, db):
    admin, org, org_admin, viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(f"/admin/organizations/{org.id}/suspend", data={"csrf_token": csrf, "reason": "   "}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Organization, org.id).status == "active"  # nicio tranzitie aplicata
