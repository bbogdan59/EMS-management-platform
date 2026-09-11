"""Regresii pentru issue #8: validare stricta (numere finite, domenii, NaN/
Infinity respinse), fara salvari partiale la esec, grupuri PV multiple
pastrate, coordonate colectate la crearea statiei, tinte SOC + suspendare
automatizare cu flux complet in UI, si concurenta optimista (versiuni)."""
from __future__ import annotations

import json
from datetime import UTC

from sqlalchemy import func, select

from app.core.rate_limit import reset_key
from app.models.organization import Membership
from app.models.preference import PreferenceVersion
from app.models.station import PanelGroup, Station, StationConfigVersion
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _config_payload(station, **overrides):
    # make_station/create_station always creates a v1 config + preference
    # automatically -- expected_version must start at 1, not 0, or every
    # first edit in these tests would itself trip the concurrent-edit check.
    payload = {
        "csrf_token": "",
        "pv_installed_power_kw": "6",
        "inverter_power_kw": "6",
        "battery_charge_efficiency": "0.95",
        "battery_discharge_efficiency": "0.95",
        "panel_groups_json": json.dumps([{"name": "Sud", "power_kwp": "6", "azimuth_degrees": "180", "tilt_degrees": "30"}]),
        "expected_version": "1",
    }
    payload.update(overrides)
    return payload


def _preferences_payload(station, **overrides):
    payload = {
        "csrf_token": "",
        "min_reserve_soc_percent": "15",
        "max_normal_soc_percent": "95",
        "priority": "cost",
        "soc_targets_json": "[]",
        "arbitrage_min_benefit_lei": "0",
        "expected_version": "1",
    }
    payload.update(overrides)
    return payload


def _latest_config(db, station_id):
    return db.scalar(
        select(StationConfigVersion).where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc()).limit(1)
    )


def _latest_preference(db, station_id):
    return db.scalar(
        select(PreferenceVersion).where(PreferenceVersion.station_id == station_id)
        .order_by(PreferenceVersion.version.desc()).limit(1)
    )


def _admin(db, name_suffix, role="organization_admin"):
    reset_key("login_attempts:testclient")
    user = make_user(db, email=f"cfg-{name_suffix}@test.local", password="Password1234")
    org = make_org(db, f"Cfg Org {name_suffix}")
    station = make_station(db, org, user, name=f"Cfg Station {name_suffix}")
    # make_station's created_by is not itself a membership -- must grant explicitly.
    make_membership(db, user, org, role=role)
    db.commit()
    return user, org, station


def test_config_save_rejects_non_numeric_input_without_partial_save(client, db):
    user, org, station = _admin(db, "nonnumeric")
    login(client, "cfg-nonnumeric@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    before = db.scalar(select(func.count(StationConfigVersion.id)).where(StationConfigVersion.station_id == station.id))
    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(station, csrf_token=csrf, pv_installed_power_kw="abc"),
    )
    assert resp.status_code == 200  # redirected back to the form with errors
    assert "Nu s-a salvat" in resp.text
    after = db.scalar(select(func.count(StationConfigVersion.id)).where(StationConfigVersion.station_id == station.id))
    assert after == before


def test_config_save_rejects_nan_and_infinity(client, db):
    user, org, station = _admin(db, "naninf")
    login(client, "cfg-naninf@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    before = db.scalar(select(func.count(StationConfigVersion.id)).where(StationConfigVersion.station_id == station.id))
    for bad_value in ("nan", "inf", "-infinity"):
        resp = client.post(
            f"/stations/{station.id}/config",
            data=_config_payload(station, csrf_token=csrf, pv_installed_power_kw=bad_value),
        )
        assert resp.status_code == 200
    after = db.scalar(select(func.count(StationConfigVersion.id)).where(StationConfigVersion.station_id == station.id))
    assert after == before, "NaN/Infinity nu trebuie sa ajunga niciodata persistat"


def test_config_save_rejects_zero_and_out_of_range_efficiency(client, db):
    user, org, station = _admin(db, "zerorange")
    login(client, "cfg-zerorange@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp_zero = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(station, csrf_token=csrf, pv_installed_power_kw="0"),
    )
    assert "Nu s-a salvat" in resp_zero.text

    resp_eff = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(station, csrf_token=csrf, battery_charge_efficiency="1.5"),
    )
    assert "Nu s-a salvat" in resp_eff.text


def test_config_save_persists_multiple_panel_groups(client, db):
    user, org, station = _admin(db, "panelgroups")
    login(client, "cfg-panelgroups@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    groups = [
        {"name": "Acoperis Sud", "power_kwp": "4", "azimuth_degrees": "180", "tilt_degrees": "25"},
        {"name": "Acoperis Est", "power_kwp": "2", "azimuth_degrees": "90", "tilt_degrees": "35"},
    ]
    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(station, csrf_token=csrf, pv_installed_power_kw="6", panel_groups_json=json.dumps(groups)),
    )
    assert "Nu s-a salvat" not in resp.text
    config = _latest_config(db, station.id)
    assert config.version == 2
    persisted = db.scalars(select(PanelGroup).where(PanelGroup.config_version_id == config.id)).all()
    assert len(persisted) == 2
    assert {g.name for g in persisted} == {"Acoperis Sud", "Acoperis Est"}


def test_config_preserves_explicit_zero_available_capacity(client, db):
    user, org, station = _admin(db, "zeroavailable")
    login(client, user.email, "Password1234")
    response = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(
            station,
            csrf_token=client.cookies.get("ems_csrf"),
            battery_reference_capacity_kwh="10",
            battery_available_capacity_kwh="0",
        ),
    )
    assert "Nu s-a salvat" not in response.text
    assert _latest_config(db, station.id).battery_available_capacity_kwh == 0


def test_config_rejects_panel_total_different_from_installed_power(client, db):
    user, org, station = _admin(db, "paneltotal")
    login(client, user.email, "Password1234")
    groups = [{"name": "Sud", "power_kwp": "5", "azimuth_degrees": "180", "tilt_degrees": "30"}]
    response = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(station, csrf_token=client.cookies.get("ems_csrf"), panel_groups_json=json.dumps(groups)),
    )
    assert "Suma puterilor grupurilor PV" in response.text


def test_config_save_detects_concurrent_edit(client, db):
    user, org, station = _admin(db, "concurrent")
    login(client, "cfg-concurrent@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    # make_station already created v1; a first real edit here (expected_version=1) creates v2.
    first = client.post(f"/stations/{station.id}/config", data=_config_payload(station, csrf_token=csrf))
    assert "Nu s-a salvat" not in first.text
    config = _latest_config(db, station.id)
    assert config.version == 2

    # Reincercare cu expected_version invechit (1, versiunea de dinainte de "first")
    # -- trebuie respinsa explicit, nu suprascrisa tacit peste v2.
    stale = client.post(f"/stations/{station.id}/config", data=_config_payload(station, csrf_token=csrf, expected_version="1"))
    assert "modificata intre timp" in stale.text
    count = db.scalar(select(func.count(StationConfigVersion.id)).where(StationConfigVersion.station_id == station.id))
    assert count == 2

    # Cu expected_version corect (2), editarea reuseste si creeaza v3.
    ok = client.post(f"/stations/{station.id}/config", data=_config_payload(station, csrf_token=csrf, expected_version="2"))
    assert "Nu s-a salvat" not in ok.text
    count2 = db.scalar(select(func.count(StationConfigVersion.id)).where(StationConfigVersion.station_id == station.id))
    assert count2 == 3


def test_preferences_reject_min_soc_above_max_soc(client, db):
    user, org, station = _admin(db, "socbounds", role="operator")
    login(client, "cfg-socbounds@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    before = db.scalar(select(func.count(PreferenceVersion.id)).where(PreferenceVersion.station_id == station.id))
    resp = client.post(
        f"/stations/{station.id}/preferences",
        data=_preferences_payload(station, csrf_token=csrf, min_reserve_soc_percent="90", max_normal_soc_percent="20"),
    )
    assert "Nu s-a salvat" in resp.text
    after = db.scalar(select(func.count(PreferenceVersion.id)).where(PreferenceVersion.station_id == station.id))
    assert after == before


def test_preferences_soc_targets_round_trip_including_near_midnight(client, db):
    user, org, station = _admin(db, "soctargets", role="operator")
    login(client, "cfg-soctargets@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    targets = [
        {"time": "18:00", "target_soc_percent": "80", "days_of_week": [0, 1, 2, 3, 4]},
        {"time": "23:45", "target_soc_percent": "100", "days_of_week": None},
        {"time": "00:15", "target_soc_percent": "50", "days_of_week": [5, 6]},
    ]
    resp = client.post(
        f"/stations/{station.id}/preferences",
        data=_preferences_payload(station, csrf_token=csrf, soc_targets_json=json.dumps(targets)),
    )
    assert "Nu s-a salvat" not in resp.text
    pref = _latest_preference(db, station.id)
    assert pref is not None
    saved_times = {t["time"] for t in pref.soc_targets}
    assert saved_times == {"18:00", "23:45", "00:15"}


def test_preferences_reject_invalid_soc_target_time(client, db):
    user, org, station = _admin(db, "socbadtime", role="operator")
    login(client, "cfg-socbadtime@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    before = db.scalar(select(func.count(PreferenceVersion.id)).where(PreferenceVersion.station_id == station.id))
    resp = client.post(
        f"/stations/{station.id}/preferences",
        data=_preferences_payload(station, csrf_token=csrf, soc_targets_json=json.dumps([{"time": "25:99", "target_soc_percent": "50"}])),
    )
    assert "Nu s-a salvat" in resp.text
    after = db.scalar(select(func.count(PreferenceVersion.id)).where(PreferenceVersion.station_id == station.id))
    assert after == before


def test_preferences_automation_suspension_round_trips_through_station_timezone(client, db):
    user, org, station = _admin(db, "suspend", role="operator")
    station.timezone = "Europe/Bucharest"
    db.add(station)
    db.commit()
    login(client, "cfg-suspend@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    local_str = "2026-12-15T14:30"
    resp = client.post(
        f"/stations/{station.id}/preferences",
        data=_preferences_payload(station, csrf_token=csrf, automation_suspended_until=local_str),
    )
    assert "Nu s-a salvat" not in resp.text
    pref = _latest_preference(db, station.id)
    assert pref.version == 2
    assert pref.automation_suspended_until is not None
    # Decembrie in Bucuresti = EET (UTC+2, fara ora de vara).
    assert pref.automation_suspended_until.astimezone(UTC).hour == 12

    # Un formular trimis cu campul gol reia automatizarea (sterge suspendarea).
    resp2 = client.post(
        f"/stations/{station.id}/preferences",
        data=_preferences_payload(station, csrf_token=csrf, automation_suspended_until="", expected_version="2"),
    )
    assert "Nu s-a salvat" not in resp2.text
    pref2 = db.scalar(
        select(PreferenceVersion).where(PreferenceVersion.station_id == station.id).order_by(PreferenceVersion.version.desc())
    )
    assert pref2.automation_suspended_until is None


def test_preferences_rejects_nonexistent_dst_local_time(client, db):
    user, org, station = _admin(db, "dstgap", role="operator")
    login(client, user.email, "Password1234")
    response = client.post(
        f"/stations/{station.id}/preferences",
        data=_preferences_payload(
            station,
            csrf_token=client.cookies.get("ems_csrf"),
            automation_suspended_until="2026-03-29T03:30",
        ),
    )
    assert "inexistenta sau ambigua" in response.text


def test_station_creation_requires_valid_coordinates(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="orgcreate1@test.local", password="Password1234")
    org = make_org(db, "Create Org 1")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    login(client, "orgcreate1@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    before = db.scalar(select(func.count(Station.id)).where(Station.organization_id == org.id))
    bad = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": csrf, "name": "No Coords Station", "timezone": "Europe/Bucharest",
            "latitude": "999", "longitude": "26.10",
            "pv_installed_power_kw": "5", "inverter_power_kw": "5",
        },
    )
    assert "Nu s-a putut salva" in bad.text
    after = db.scalar(select(func.count(Station.id)).where(Station.organization_id == org.id))
    assert after == before

    good = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": csrf, "name": "Coords Station", "timezone": "Europe/Bucharest",
            "latitude": "44.43", "longitude": "26.10",
            "pv_installed_power_kw": "5", "inverter_power_kw": "5",
        },
    )
    assert good.status_code == 200
    station = db.scalar(select(Station).where(Station.organization_id == org.id, Station.name == "Coords Station"))
    assert station is not None
    assert float(station.latitude) == 44.43
    assert float(station.longitude) == 26.10


def test_station_creation_rejects_unknown_timezone(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="orgtimezone@test.local", password="Password1234")
    org = make_org(db, "Timezone Org")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    login(client, user.email, "Password1234")
    response = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": client.cookies.get("ems_csrf"),
            "name": "Bad timezone",
            "timezone": "Mars/Olympus",
            "latitude": "44.43",
            "longitude": "26.10",
            "pv_installed_power_kw": "5",
            "inverter_power_kw": "5",
        },
    )
    assert "Fus orar IANA invalid" in response.text


def test_operator_cannot_manage_station_config_deterministic(client, db):
    """Versiune deterministica, in fisierul propriu al acestui issue, a
    verificarii RBAC -- vezi docs/LIMITATIONS.md pentru nota despre flakiness-ul
    observat ocazional in suita completa pentru testul echivalent din
    test_org_isolation.py (comportamentul StationAccess/role_at_least insusi
    verificat corect, izolat, cu instrumentare directa)."""
    user, org, station = _admin(db, "rbac-op", role="operator")
    login(client, "cfg-rbac-op@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    view = client.get(f"/stations/{station.id}/config")
    assert view.status_code == 200

    forbidden = client.post(f"/stations/{station.id}/config", data=_config_payload(station, csrf_token=csrf))
    assert forbidden.status_code == 403

    allowed = client.post(f"/stations/{station.id}/preferences", data=_preferences_payload(station, csrf_token=csrf))
    assert allowed.status_code == 200


def test_viewer_cannot_edit_preferences(client, db):
    user, org, station = _admin(db, "rbac-viewer", role="viewer")
    login(client, "cfg-rbac-viewer@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    forbidden = client.post(f"/stations/{station.id}/preferences", data=_preferences_payload(station, csrf_token=csrf))
    assert forbidden.status_code == 403


def test_config_race_produces_no_duplicate_versions(engine):
    """Constrangerea unica (station_id, version) plus verificarea optimista
    expected_version trebuie sa impiedice doua editari concurente sa produca
    aceeasi versiune -- test real cu doua sesiuni/thread-uri pe engine-ul de
    test (nu fixtura `db` cu SAVEPOINT, care nu poate exercita commit-uri
    concurente reale)."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.models.organization import Organization
    from app.models.user import User
    from app.services import station_service

    suffix = uuid4().hex
    with Session(engine) as setup:
        user = make_user(setup, email=f"{suffix}@cfgrace.test")
        org = make_org(setup, f"Cfg Race {suffix}")
        station = make_station(setup, org, user, name=f"Cfg Race Station {suffix}")
        setup.commit()
        station_id, org_id, user_id = station.id, org.id, user.id

    barrier = Barrier(2)

    def attempt():
        with Session(engine) as session:
            station = session.get(Station, station_id)
            current_version = station_service.next_config_version(session, station) - 1
            barrier.wait(timeout=5)
            config = StationConfigVersion(
                station_id=station_id, version=current_version + 1, created_by_user_id=user_id,
                pv_installed_power_kw=5, inverter_power_kw=5,
                battery_charge_efficiency="0.95", battery_discharge_efficiency="0.95",
            )
            session.add(config)
            try:
                session.commit()
                return "ok"
            except Exception:
                session.rollback()
                return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            outcomes = [f.result(timeout=10) for f in futures]
        assert "ok" in outcomes

        with Session(engine) as verify:
            versions = [
                c.version for c in verify.scalars(select(StationConfigVersion).where(StationConfigVersion.station_id == station_id)).all()
            ]
            assert len(versions) == len(set(versions)), "nicio versiune duplicata"
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(StationConfigVersion).where(StationConfigVersion.station_id == station_id))
            cleanup.execute(delete(Membership).where(Membership.user_id == user_id))
            cleanup.execute(delete(Station).where(Station.id == station_id))
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
