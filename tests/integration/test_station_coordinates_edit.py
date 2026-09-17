"""Regresie pentru issue #132: `latitude`/`longitude` (pe `Station`, nu
versionate) pot fi corectate dupa crearea statiei, din pagina de configurare
tehnica -- anterior existau doar la creare, iar un typo degrada permanent
prognoza PV fara nicio cale de corectie din UI."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.station import Station, StationConfigVersion
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _config_payload(**overrides):
    payload = {
        "csrf_token": "",
        "pv_installed_power_kw": "6",
        "inverter_power_kw": "6",
        "latitude": "44.43",
        "longitude": "26.10",
        "battery_charge_efficiency": "0.95",
        "battery_discharge_efficiency": "0.95",
        "panel_groups_json": json.dumps([{"name": "Sud", "power_kwp": "6", "azimuth_degrees": "180", "tilt_degrees": "30"}]),
        "expected_version": "1",
    }
    payload.update(overrides)
    return payload


def _admin(db, suffix):
    reset_key("login_attempts:testclient")
    user = make_user(db, email=f"coords-{suffix}@test.local", password="Password1234")
    org = make_org(db, f"Coords Org {suffix}")
    station = make_station(db, org, user, name=f"Coords Station {suffix}")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    return user, org, station


def test_config_form_prefills_current_station_coordinates(client, db):
    user, org, station = _admin(db, "prefill")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200
    assert 'name="latitude"' in resp.text
    assert 'name="longitude"' in resp.text
    assert 'value="44.43"' in resp.text
    assert 'value="26.10"' in resp.text


def test_config_submit_corrects_station_coordinates(client, db):
    user, org, station = _admin(db, "correct")
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, latitude="45.75", longitude="21.23"),
    )
    assert "Nu s-a salvat" not in resp.text
    db.expire_all()
    updated = db.get(Station, station.id)
    assert float(updated.latitude) == 45.75
    assert float(updated.longitude) == 21.23


def test_config_submit_rejects_out_of_range_coordinates_without_partial_save(client, db):
    user, org, station = _admin(db, "outofrange")
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    before_versions = db.scalar(select(StationConfigVersion.id).where(StationConfigVersion.station_id == station.id))
    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, latitude="999", longitude="26.10"),
    )
    assert "Nu s-a salvat" in resp.text
    db.expire_all()
    unchanged = db.get(Station, station.id)
    assert float(unchanged.latitude) == 44.43  # nicio salvare partiala
    after_versions = db.scalar(select(StationConfigVersion.id).where(StationConfigVersion.station_id == station.id))
    assert before_versions == after_versions


def test_setup_summary_shows_degree_unit_for_coordinates(client, db):
    user, org, station = _admin(db, "summary")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/setup/summary")
    assert resp.status_code == 200
    assert "44.43°" in resp.text
    assert "26.10°" in resp.text
