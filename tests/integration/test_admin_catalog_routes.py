"""Teste HTTP pentru backoffice-ul catalogului de echipamente (issue #42):
CRUD producator/model, activare/dezactivare nedistructiva, si RBAC
(exclusiv platform_admin -- niciun rol de organizatie nu poate administra
catalogul, care e o capabilitate de platforma, nu per-tenant)."""
from __future__ import annotations

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.equipment_catalog import EquipmentManufacturer, EquipmentModel
from tests.factories import (
    make_equipment_model,
    make_manufacturer,
    make_membership,
    make_org,
    make_user,
)
from tests.web_helpers import login


def _setup(db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="catalog-admin@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Catalog Routes Co")
    org_admin = make_user(db, email="catalog-orgadmin@test.local", password="Password1234")
    make_membership(db, org_admin, org, role="organization_admin")
    db.commit()
    return admin, org_admin


def test_catalog_page_renders_manufacturers_and_models(client, db):
    admin, _org_admin = _setup(db)
    manufacturer = make_manufacturer(db, name="Deye")
    make_equipment_model(db, manufacturer=manufacturer, equipment_type="inverter", model_name="X1", specs={"a": 1})
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get("/admin/catalog")
    assert resp.status_code == 200
    assert "Deye" in resp.text
    assert "X1" in resp.text


def test_catalog_page_requires_platform_admin(client, db):
    _admin, org_admin = _setup(db)
    login(client, "catalog-orgadmin@test.local", "Password1234")
    resp = client.get("/admin/catalog", follow_redirects=False)
    assert resp.status_code in (303, 401, 403)


def test_platform_admin_creates_manufacturer(client, db):
    _setup(db)
    login(client, "catalog-admin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/catalog/manufacturers", data={"csrf_token": csrf, "name": "Deye"}, follow_redirects=False
    )
    assert resp.status_code == 303

    manufacturer = db.scalar(select(EquipmentManufacturer).where(EquipmentManufacturer.name == "Deye"))
    assert manufacturer is not None
    assert manufacturer.is_active is True


def test_duplicate_manufacturer_name_rejected(client, db):
    _setup(db)
    make_manufacturer(db, name="Deye")
    db.commit()
    login(client, "catalog-admin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/catalog/manufacturers", data={"csrf_token": csrf, "name": "Deye"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert db.scalar(select(EquipmentManufacturer).where(EquipmentManufacturer.name == "Deye")) is not None


def test_platform_admin_creates_equipment_model(client, db):
    _setup(db)
    manufacturer = make_manufacturer(db, name="Deye")
    db.commit()
    login(client, "catalog-admin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/catalog/models",
        data={
            "csrf_token": csrf, "manufacturer_id": str(manufacturer.id), "equipment_type": "inverter",
            "model_name": "SUN-10K-SG04LP3", "source_note": "fisa tehnica producator",
            "specs_json": '{"power_kw": 10}',
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    model = db.scalar(select(EquipmentModel).where(EquipmentModel.model_name == "SUN-10K-SG04LP3"))
    assert model is not None
    assert model.specs == {"power_kw": 10}
    assert model.spec_revision == 1


def test_equipment_model_requires_valid_json_specs(client, db):
    _setup(db)
    manufacturer = make_manufacturer(db, name="Deye")
    db.commit()
    login(client, "catalog-admin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/catalog/models",
        data={
            "csrf_token": csrf, "manufacturer_id": str(manufacturer.id), "equipment_type": "inverter",
            "model_name": "X", "source_note": "src", "specs_json": "not json",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert db.scalar(select(EquipmentModel).where(EquipmentModel.model_name == "X")) is None


def test_toggle_model_active_is_nondestructive(client, db):
    _setup(db)
    model = make_equipment_model(db)
    db.commit()
    login(client, "catalog-admin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(f"/admin/catalog/models/{model.id}/toggle-active", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.refresh(model)
    assert model.is_active is False
    # Randul ramane -- doar dezactivat, nu sters.
    assert db.get(EquipmentModel, model.id) is not None


def test_update_specs_publishes_new_revision(client, db):
    _setup(db)
    model = make_equipment_model(db, specs={"power_kw": 10}, spec_revision=1)
    db.commit()
    login(client, "catalog-admin@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/admin/catalog/models/{model.id}/specs",
        data={"csrf_token": csrf, "source_note": "fisa v2", "specs_json": '{"power_kw": 12}'},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(model)
    assert model.spec_revision == 2
    assert model.specs == {"power_kw": 12}
