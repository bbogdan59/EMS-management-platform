from datetime import UTC, datetime
from decimal import Decimal

from app.web.routes import dashboard
from tests.factories import make_membership, make_org, make_station, make_user
from tests.integration.test_station_coordinates_edit import _config_payload
from tests.web_helpers import login


def _station(db, role="viewer"):
    user = make_user(db)
    org = make_org(db)
    station = make_station(db, org, user)
    make_membership(db, user, org, role=role)
    db.commit()
    return user, station


def test_viewer_can_read_current_sun_in_station_timezone(client, db, monkeypatch):
    user, station = _station(db)
    monkeypatch.setattr(dashboard, "utcnow", lambda: datetime(2026, 6, 21, 22, tzinfo=UTC))
    login(client, user.email, "TestPass1234")
    response = client.get(f"/stations/{station.id}/data/sun")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["local_date"] == "2026-06-22"
    assert data["timezone"] == "Europe/Bucharest"
    assert data["location"] == {"latitude": 44.43, "longitude": 26.1}
    assert data["current"]["is_daylight"] is False
    assert data["source"] == "calculated"


def test_sun_endpoint_rejects_another_tenant(client, db):
    user, station = _station(db)
    other_org = make_org(db, "Other tenant")
    other = make_station(db, other_org, user)
    db.commit()
    login(client, user.email, "TestPass1234")
    assert client.get(f"/stations/{other.id}/data/sun").status_code == 403


def test_sun_endpoint_requires_login(client, db):
    user, station = _station(db)
    response = client.get(f"/stations/{station.id}/data/sun", follow_redirects=False)
    assert response.status_code in (401, 302, 303)


def test_missing_location_is_explicit_in_api(client, db):
    user, station = _station(db)
    station.latitude = None
    db.commit()
    login(client, user.email, "TestPass1234")
    data = client.get(f"/stations/{station.id}/data/sun").json()
    assert data["status"] == "missing_location"
    assert data["current"] is None


def test_pin_coordinates_saved_at_full_precision_are_used_by_sun_map(client, db):
    user, station = _station(db, role="organization_admin")
    login(client, user.email, "TestPass1234")
    response = client.post(f"/stations/{station.id}/config", data=_config_payload(
        csrf_token=client.cookies.get("ems_csrf"), latitude="45.123456", longitude="25.654321",
    ))
    assert response.status_code == 200
    db.refresh(station)
    assert station.latitude == Decimal("45.123456")
    assert station.longitude == Decimal("25.654321")
    assert client.get(f"/stations/{station.id}/data/sun").json()["location"] == {
        "latitude": 45.123456, "longitude": 25.654321,
    }


def test_location_picker_is_only_available_to_station_admin(client, db):
    user, station = _station(db)
    login(client, user.email, "TestPass1234")
    assert "data-location-picker" not in client.get(f"/stations/{station.id}/config").text
    response = client.post(f"/stations/{station.id}/config", data=_config_payload(
        csrf_token=client.cookies.get("ems_csrf"), latitude="0", longitude="0",
    ))
    assert response.status_code == 403
    db.refresh(station)
    assert station.latitude == Decimal("44.43")


def test_pin_save_still_requires_csrf(client, db):
    user, station = _station(db, role="organization_admin")
    login(client, user.email, "TestPass1234")
    response = client.post(f"/stations/{station.id}/config", data=_config_payload(latitude="0", longitude="0"))
    assert response.status_code == 403
    db.refresh(station)
    assert station.latitude == Decimal("44.43")
