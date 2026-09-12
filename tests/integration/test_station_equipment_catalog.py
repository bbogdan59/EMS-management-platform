"""Integrarea catalogului administrabil de echipamente (issue #42) in
formularul de configurare a statiei: selectie din catalog cu snapshot
imutabil, fallback custom pentru un model care lipseste, si search-ul
folosit de widget-ul de autocomplete."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.station import PanelGroup, StationConfigVersion
from app.services import equipment_catalog_service
from tests.factories import (
    make_equipment_model,
    make_manufacturer,
    make_membership,
    make_org,
    make_station,
    make_user,
)
from tests.web_helpers import login


def _admin(db, name_suffix):
    reset_key("login_attempts:testclient")
    user = make_user(db, email=f"eqcat-{name_suffix}@test.local", password="Password1234")
    org = make_org(db, f"EqCat Org {name_suffix}")
    station = make_station(db, org, user, name=f"EqCat Station {name_suffix}")
    make_membership(db, user, org, role="organization_admin")
    db.commit()
    return user, org, station


def _config_payload(**overrides):
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


def _latest_config(db, station_id):
    return db.scalar(
        select(StationConfigVersion).where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc()).limit(1)
    )


def test_config_page_renders_equipment_search_widgets(client, db):
    user, _org, station = _admin(db, "rendersmoke")
    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200
    assert "Model invertor" in resp.text
    assert "Model baterie" in resp.text
    assert "Model panou" in resp.text


def test_config_persists_catalog_model_snapshot_for_inverter_and_battery(client, db):
    user, org, station = _admin(db, "snapshot")
    manufacturer = make_manufacturer(db, name="Deye")
    inverter_model = make_equipment_model(
        db, manufacturer=manufacturer, equipment_type="inverter", model_name="SUN-10K-SG04LP3",
        specs={"power_kw": 10}, source_note="fisa",
    )
    battery_model = make_equipment_model(
        db, manufacturer=manufacturer, equipment_type="battery", model_name="SE-G5.1-Pro",
        specs={"capacity_kwh": 5.12}, source_note="fisa",
    )
    db.commit()
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, inverter_model_id=str(inverter_model.id), battery_model_id=str(battery_model.id)),
    )
    assert "Nu s-a salvat" not in resp.text

    config = _latest_config(db, station.id)
    assert config.inverter_model_id == inverter_model.id
    assert config.inverter_model_spec_revision == 1
    assert config.inverter_model_snapshot["manufacturer"] == "Deye"
    assert config.inverter_model_snapshot["specs"] == {"power_kw": 10}
    assert config.inverter_custom_label is None

    assert config.battery_model_id == battery_model.id
    assert config.battery_model_snapshot["model_name"] == "SE-G5.1-Pro"
    assert config.battery_custom_label is None


def test_config_persists_custom_label_without_catalog_model(client, db):
    user, org, station = _admin(db, "custom")
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, inverter_custom_label="Invertor generic necunoscut"),
    )
    assert "Nu s-a salvat" not in resp.text

    config = _latest_config(db, station.id)
    assert config.inverter_model_id is None
    assert config.inverter_model_snapshot is None
    assert config.inverter_custom_label == "Invertor generic necunoscut"


def test_config_rejects_both_catalog_model_and_custom_label(client, db):
    user, org, station = _admin(db, "bothset")
    model = make_equipment_model(db, equipment_type="inverter")
    db.commit()
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, inverter_model_id=str(model.id), inverter_custom_label="Nume custom"),
    )
    assert "Nu s-a salvat" in resp.text
    assert "de catalog" in resp.text


def test_config_rejects_unknown_equipment_model_id(client, db):
    import uuid

    user, org, station = _admin(db, "unknownmodel")
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, inverter_model_id=str(uuid.uuid4())),
    )
    assert "nu a fost gasit" in resp.text


def test_config_rejects_model_id_with_wrong_equipment_type(client, db):
    """Un model de tip 'battery' folosit ca `inverter_model_id` trebuie
    respins -- nu doar orice UUID existent e valid, ci unul de tipul corect."""
    user, org, station = _admin(db, "wrongtype")
    battery_model = make_equipment_model(db, equipment_type="battery")
    db.commit()
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, inverter_model_id=str(battery_model.id)),
    )
    assert "nu a fost gasit" in resp.text


def test_config_panel_group_persists_pv_module_model_and_count(client, db):
    user, org, station = _admin(db, "pvmodule")
    pv_model = make_equipment_model(db, equipment_type="pv_module", model_name="JAM72S30", specs={"power_wp": 550})
    db.commit()
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    groups = [{
        "name": "Sud", "power_kwp": "6", "azimuth_degrees": "180", "tilt_degrees": "30",
        "pv_module_model_id": str(pv_model.id), "pv_module_count": 11,
    }]
    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, panel_groups_json=json.dumps(groups)),
    )
    assert "Nu s-a salvat" not in resp.text

    config = _latest_config(db, station.id)
    group = db.scalar(select(PanelGroup).where(PanelGroup.config_version_id == config.id))
    assert group.pv_module_model_id == pv_model.id
    assert group.pv_module_count == 11
    assert group.pv_module_model_snapshot["model_name"] == "JAM72S30"


def test_catalog_edit_does_not_change_already_published_snapshot(client, db):
    """Contractul central al issue #42: editarea specificatiilor unui model
    DUPA ce a fost selectat intr-o configuratie publicata NU modifica
    retroactiv acea configuratie -- doar selectiile viitoare vad noile
    valori."""
    user, org, station = _admin(db, "immutable")
    inverter_model = make_equipment_model(db, equipment_type="inverter", specs={"power_kw": 10})
    db.commit()
    login(client, user.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/stations/{station.id}/config",
        data=_config_payload(csrf_token=csrf, inverter_model_id=str(inverter_model.id)),
    )
    assert "Nu s-a salvat" not in resp.text
    config = _latest_config(db, station.id)
    assert config.inverter_model_snapshot["specs"] == {"power_kw": 10}
    assert config.inverter_model_spec_revision == 1

    equipment_catalog_service.update_equipment_model_specs(
        db, user, inverter_model, specs={"power_kw": 12}, source_note="fisa v2"
    )
    db.commit()

    db.refresh(config)
    assert config.inverter_model_snapshot["specs"] == {"power_kw": 10}
    assert config.inverter_model_spec_revision == 1


def test_equipment_search_filters_by_type_and_excludes_inactive(client, db):
    user, _org, _station = _admin(db, "search")
    manufacturer = make_manufacturer(db, name="Deye")
    active = make_equipment_model(db, manufacturer=manufacturer, equipment_type="inverter", model_name="Active-1")
    make_equipment_model(db, manufacturer=manufacturer, equipment_type="inverter", model_name="Inactive-1", is_active=False)
    make_equipment_model(db, manufacturer=manufacturer, equipment_type="battery", model_name="Battery-1")
    db.commit()
    login(client, user.email, "Password1234")

    resp = client.get("/equipment/search", params={"equipment_type": "inverter"})
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert ids == {str(active.id)}
