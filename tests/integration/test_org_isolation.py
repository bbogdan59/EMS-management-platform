from __future__ import annotations

from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def test_user_can_only_see_own_organization_stations(client, db):
    user1 = make_user(db, email="iso1@test.local", password="Password1234")
    user2 = make_user(db, email="iso2@test.local", password="Password1234")
    org1 = make_org(db, "Iso Org 1")
    org2 = make_org(db, "Iso Org 2")
    make_membership(db, user1, org1, role="viewer")
    make_membership(db, user2, org2, role="viewer")
    station1 = make_station(db, org1, user1, name="Station Org1")
    station2 = make_station(db, org2, user2, name="Station Org2")
    db.commit()

    resp = login(client, "iso1@test.local", "Password1234")
    assert resp.status_code == 303

    home = client.get(f"/?station_id={station1.id}")
    assert home.status_code == 200
    assert b"Station Org1" in home.content

    forbidden = client.get(f"/stations/{station2.id}/config")
    assert forbidden.status_code == 403


def test_platform_admin_can_access_any_station(client, db):
    admin = make_user(db, email="platadmin@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Iso Org 3")
    station = make_station(db, org, admin, name="Station Org3")
    db.commit()

    login(client, "platadmin@test.local", "Password1234")
    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200


def test_operator_cannot_manage_station_config_but_can_view(client, db):
    user = make_user(db, email="operator1@test.local", password="Password1234")
    org = make_org(db, "Iso Org 4")
    station = make_station(db, org, user, name="Station Org4")
    make_membership(db, user, org, role="operator")
    db.commit()

    login(client, "operator1@test.local", "Password1234")
    view_resp = client.get(f"/stations/{station.id}/config")
    assert view_resp.status_code == 200

    csrf = client.cookies.get("ems_csrf")
    post_resp = client.post(
        f"/stations/{station.id}/config",
        data={"csrf_token": csrf, "pv_installed_power_kw": "9", "inverter_power_kw": "9"},
    )
    assert post_resp.status_code == 403
