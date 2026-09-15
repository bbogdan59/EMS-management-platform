"""Teste comportamentale pentru wizard-ul multi-pas de configurare organizatie/
statie (issue #41): navigare Back/Next/skip intre pasii deja existenti,
reluare (banner pe dashboard cand configurarea nu e finalizata), activare
idempotenta si RBAC/izolare cross-tenant pe rutele noi de wizard.

Design deliberat testat aici: wizard-ul NU introduce validare noua -- fiecare
pas ramane exact ruta existenta (`create_station`/`station_config_submit`/
`activate_device_code`/`tariffs_submit`/`preferences_submit`), doar cu chrome
si redirect-uri suplimentare cand vine `wizard=1`. Testele de validare stricta
per camp raman in `test_station_config_forms.py`/`test_tariffs_routes.py`."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.device import Device
from app.models.station import Station
from app.models.tariff import Tariff
from app.services import device_service, station_service
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import get_csrf, login


def _setup(db, *, role="organization_admin", suffix="1"):
    reset_key("login_attempts:testclient")
    org = make_org(db, f"Wizard Org {suffix}")
    admin = make_user(db, email=f"wizard-admin-{suffix}@test.local", password="Password1234")
    make_membership(db, admin, org, role=role)
    db.commit()
    return org, admin


def _config_payload(**overrides):
    import json

    payload = {
        "csrf_token": "",
        "pv_installed_power_kw": "6",
        "inverter_power_kw": "6",
        "battery_charge_efficiency": "0.95",
        "battery_discharge_efficiency": "0.95",
        "panel_groups_json": json.dumps([{"name": "Sud", "power_kwp": "6", "azimuth_degrees": "180", "tilt_degrees": "30"}]),
        "expected_version": "1",
        "wizard": "1",
    }
    payload.update(overrides)
    return payload


def _preferences_payload(**overrides):
    payload = {
        "csrf_token": "",
        "min_reserve_soc_percent": "15",
        "max_normal_soc_percent": "95",
        "priority": "cost",
        "soc_targets_json": "[]",
        "arbitrage_min_benefit_lei": "0",
        "expected_version": "1",
        "wizard": "1",
    }
    payload.update(overrides)
    return payload


# --- Pasul 1: creare statie prin wizard ---


def test_wizard_station_entry_page_renders_for_org_admin(client, db):
    org, admin = _setup(db)
    login(client, admin.email, "Password1234")

    resp = client.get(f"/organizations/{org.id}/setup/station")
    assert resp.status_code == 200
    assert "Statie" in resp.text
    assert "Rezumat" in resp.text  # toti pasii wizard-ului apar in progres


def test_wizard_station_entry_page_forbidden_for_other_org(client, db):
    org1, _admin1 = _setup(db, suffix="1")
    _org2, admin2 = _setup(db, role="viewer", suffix="2")
    login(client, admin2.email, "Password1234")

    resp = client.get(f"/organizations/{org1.id}/setup/station")
    assert resp.status_code == 403


def test_wizard_create_station_redirects_into_config_step(client, db):
    org, admin = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": csrf, "name": "Wizard Station", "timezone": "Europe/Bucharest",
            "latitude": "44.43", "longitude": "26.10",
            "pv_installed_power_kw": "5", "inverter_power_kw": "5",
            "wizard": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    station = db.scalar(select(Station).where(Station.name == "Wizard Station"))
    assert station is not None
    assert resp.headers["location"] == f"/stations/{station.id}/config?wizard=1"


def test_wizard_create_station_validation_error_returns_to_wizard_entry(client, db):
    org, admin = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/organizations/{org.id}/stations",
        data={
            "csrf_token": csrf, "name": "Bad Station", "timezone": "Europe/Bucharest",
            "latitude": "999", "longitude": "26.10",  # latitudine invalida
            "pv_installed_power_kw": "5", "inverter_power_kw": "5",
            "wizard": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/organizations/{org.id}/setup/station?")

    follow = client.get(resp.headers["location"])
    assert follow.status_code == 200
    assert "Nu s-a creat statia" in follow.text
    assert db.scalar(select(Station).where(Station.name == "Bad Station")) is None


# --- Navigare Back/Next/skip intre pasii existenti ---


def test_wizard_config_step_shows_progress_and_skip_to_devices(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Nav Station")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config?wizard=1")
    assert resp.status_code == 200
    assert 'aria-current="step"' in resp.text
    assert f"/stations/{station.id}/devices?wizard=1" in resp.text  # link de skip catre pasul urmator
    assert f"/organizations/{org.id}" in resp.text  # back catre organizatie


def test_wizard_mode_requires_explicit_one_marker(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Strict Wizard Marker")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config?wizard=0")

    assert resp.status_code == 200
    assert 'aria-current="step"' not in resp.text


def test_wizard_config_submit_advances_to_devices_step(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Config Advance Station")
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/config", data=_config_payload(csrf_token=csrf), follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/stations/{station.id}/devices?wizard=1"


def test_wizard_config_submit_error_preserves_wizard_mode(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Config Error Station")
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, pv_installed_power_kw="-1"),  # invalid: trebuie strict pozitiv
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "wizard=1" in resp.headers["location"]
    follow = client.get(resp.headers["location"])
    assert 'data-error-summary' in follow.text
    assert 'aria-current="step"' in follow.text  # chrome-ul ramane afisat dupa eroare


def test_wizard_device_activation_advances_to_tariffs_step(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Device Advance Station")
    db.commit()

    installation_uuid = str(uuid.uuid4())
    code = "ACT-WIZRD-DEVCE-STEPT-EST01"
    device_service.enroll_device(
        db, installation_uuid, "provisioning-secret-wizard-000001", {},
        serial_number="EMS-WIZD-0001-0001", activation_code=code,
    )
    db.commit()

    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/stations/{station.id}/devices/activate",
        data={"csrf_token": csrf, "activation_code": code, "wizard": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/stations/{station.id}/tariffs?wizard=1"

    device = db.scalar(select(Device).where(Device.installation_uuid == installation_uuid))
    assert device.station_id == station.id


def test_wizard_devices_step_skip_link_goes_to_tariffs(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Skip Devices Station")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/devices?wizard=1")
    assert resp.status_code == 200
    assert f"/stations/{station.id}/tariffs?wizard=1" in resp.text


def test_wizard_tariffs_submit_advances_to_preferences_step(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Tariff Advance Station")
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "fixed", "name": "Fix standard",
            "fixed_price_lei_per_kwh": "0.85", "wizard": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/stations/{station.id}/preferences?wizard=1"


def test_wizard_preferences_submit_advances_to_summary(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Preferences Advance Station")
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/preferences", data=_preferences_payload(csrf_token=csrf), follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/stations/{station.id}/setup/summary"


# --- Rezumat + activare (idempotenta) ---


def test_setup_summary_shows_incomplete_sections_and_activation_button(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Summary Station")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/setup/summary")
    assert resp.status_code == 200
    assert "Niciun device conectat inca" in resp.text
    assert "Niciun tarif configurat" in resp.text
    assert "Activeaza statia" in resp.text


def test_activate_setup_is_idempotent_and_redirects_to_dashboard(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Activate Station")
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp1 = client.post(f"/stations/{station.id}/setup/activate", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp1.status_code == 303
    assert resp1.headers["location"] == f"/?station_id={station.id}"

    db.expire_all()
    station_after_first = db.get(Station, station.id)
    first_completed_at = station_after_first.setup_completed_at
    assert first_completed_at is not None

    resp2 = client.post(f"/stations/{station.id}/setup/activate", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == f"/?station_id={station.id}"

    db.expire_all()
    station_after_second = db.get(Station, station.id)
    assert station_after_second.setup_completed_at == first_completed_at  # neschimbat la al doilea submit


def test_viewer_cannot_activate_setup(client, db):
    org, viewer = _setup(db, role="viewer")
    admin = make_user(db, email="activate-admin@test.local", password="Password1234")
    make_membership(db, admin, org, role="organization_admin")
    station = make_station(db, org, admin, name="Viewer Activate Station")
    db.commit()

    login(client, viewer.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(f"/stations/{station.id}/setup/activate", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 403


# --- Reluare (banner pe dashboard) + tenant isolation ---


def test_dashboard_shows_resume_banner_when_setup_incomplete_for_org_admin(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Resume Banner Station")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/?station_id={station.id}")
    assert resp.status_code == 200
    assert "Continua configurarea" in resp.text
    assert f"/stations/{station.id}/devices?wizard=1" in resp.text  # urmatorul pas real: fara device


def test_dashboard_resume_banner_points_to_summary_once_device_and_tariff_exist(client, db):
    from tests.factories import make_device

    org, admin = _setup(db)
    station = make_station(db, org, admin, name="Resume Progress Station")
    make_device(db, station)
    db.commit()
    from datetime import UTC, datetime

    from app.services import tariff_service

    tariff = tariff_service.get_or_create_tariff(db, station, "import", "fixed", "Fix")
    tariff_service.add_tariff_version(
        db, tariff, valid_from=datetime.now(UTC), fixed_price_lei_per_kwh=None,
        opcom_margin_lei_per_kwh=None, fixed_monthly_fee_lei=0, variable_component_lei_per_kwh=0,
        distribution_lei_per_kwh=0, transport_lei_per_kwh=0, other_regulated_lei_per_kwh=0,
        vat_rate_percent=None, settlement_method="net_metering_15min", settlement_interval_days=30,
        economic_calculation_disabled=True, limitation_note="test",
    )
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/?station_id={station.id}")
    assert resp.status_code == 200
    assert "Continua configurarea" in resp.text
    assert f"/stations/{station.id}/setup/summary" in resp.text


def test_dashboard_hides_resume_banner_after_activation(client, db):
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="No Banner After Activate")
    db.commit()
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(f"/stations/{station.id}/setup/activate", data={"csrf_token": csrf})

    resp = client.get(f"/?station_id={station.id}")
    assert resp.status_code == 200
    assert "Continua configurarea" not in resp.text


def test_dashboard_never_force_redirects_incomplete_station(client, db):
    """Reluarea e o sugestie (banner), niciodata un redirect fortat -- vizitarea
    dashboard-ului unei statii incomplete tot randeaza dashboard-ul (200), nu
    trimite utilizatorul in alta parte impotriva vointei lui."""
    org, admin = _setup(db)
    station = make_station(db, org, admin, name="No Force Redirect Station")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/?station_id={station.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "No Force Redirect Station" in resp.text


def test_viewer_does_not_see_resume_banner(client, db):
    org, admin = _setup(db)
    viewer = make_user(db, email="resume-viewer@test.local", password="Password1234")
    make_membership(db, viewer, org, role="viewer")
    station = make_station(db, org, admin, name="Viewer No Banner Station")
    db.commit()

    login(client, viewer.email, "Password1234")
    resp = client.get(f"/?station_id={station.id}")
    assert resp.status_code == 200
    assert "Continua configurarea" not in resp.text


def test_cross_tenant_cannot_access_setup_summary_or_activate(client, db):
    org1, admin1 = _setup(db, suffix="1")
    station = make_station(db, org1, admin1, name="Isolated Station")
    db.commit()

    _org2, admin2 = _setup(db, role="organization_admin", suffix="2")
    login(client, admin2.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/setup/summary")
    assert resp.status_code == 403

    csrf = client.cookies.get("ems_csrf")
    resp2 = client.post(f"/stations/{station.id}/setup/activate", data={"csrf_token": csrf})
    assert resp2.status_code == 403


def test_setup_progress_helper_reflects_real_state(db):
    org = make_org(db, "Progress Helper Org")
    user = make_user(db, email="progress-helper@test.local", password="Password1234")
    station = make_station(db, org, user, name="Progress Helper Station")
    db.commit()

    progress = station_service.setup_progress(db, station)
    assert progress == {"has_device": False, "has_tariff": False, "completed": False}


def test_setup_progress_ignores_inactive_tariff(db):
    org = make_org(db, "Inactive Tariff Progress Org")
    user = make_user(db, email="inactive-tariff-progress@test.local", password="Password1234")
    station = make_station(db, org, user, name="Inactive Tariff Progress Station")
    db.add(Tariff(station_id=station.id, direction="import", kind="fixed", name="Expirat", is_active=False))
    db.commit()

    progress = station_service.setup_progress(db, station)

    assert progress["has_tariff"] is False
