from __future__ import annotations

import pytest

from app.services import equipment_catalog_service as svc
from tests.factories import make_equipment_model, make_manufacturer, make_user


def test_create_manufacturer_and_model(db):
    actor = make_user(db)
    manufacturer = svc.create_manufacturer(db, actor, name="Deye")
    assert manufacturer.is_active is True

    model = svc.create_equipment_model(
        db, actor, manufacturer=manufacturer, equipment_type="inverter", model_name="SUN-10K-SG04LP3",
        specs={"power_kw": 10}, source_note="fisa tehnica producator",
    )
    assert model.spec_revision == 1
    assert model.specs == {"power_kw": 10}
    assert model.is_active is True


def test_create_equipment_model_rejects_unknown_type(db):
    actor = make_user(db)
    manufacturer = make_manufacturer(db)
    with pytest.raises(svc.EquipmentCatalogError):
        svc.create_equipment_model(
            db, actor, manufacturer=manufacturer, equipment_type="drone",
            model_name="X", specs={}, source_note="src",
        )


def test_create_equipment_model_requires_source_note(db):
    actor = make_user(db)
    manufacturer = make_manufacturer(db)
    with pytest.raises(svc.EquipmentCatalogError):
        svc.create_equipment_model(
            db, actor, manufacturer=manufacturer, equipment_type="inverter",
            model_name="X", specs={}, source_note="   ",
        )


def test_update_specs_bumps_revision(db):
    actor = make_user(db)
    model = make_equipment_model(db, specs={"power_kw": 10}, spec_revision=1)

    svc.update_equipment_model_specs(db, actor, model, specs={"power_kw": 12}, source_note="fisa v2")
    assert model.spec_revision == 2
    assert model.specs == {"power_kw": 12}
    assert model.source_note == "fisa v2"


def test_build_snapshot_is_a_defensive_copy(db):
    """Snapshot-ul retinut de un consumator (`StationConfigVersion`) trebuie
    sa fie independent de dict-ul `model.specs` -- o mutatie ulterioara IN
    LOC a specs-urilor modelului nu trebuie sa se propage intr-un snapshot
    deja capturat (altfel `update_equipment_model_specs` ar modifica
    retroactiv configuratii deja publicate, exact ce issue #42 interzice)."""
    original_specs = {"power_kw": 10}
    model = make_equipment_model(db, specs=original_specs)

    snapshot = svc.build_snapshot(model)
    original_specs["power_kw"] = 999  # mutatie in acelasi dict, fara re-salvare prin service

    assert snapshot["specs"]["power_kw"] == 10


def test_set_model_active_is_nondestructive(db):
    actor = make_user(db)
    model = make_equipment_model(db)
    svc.set_equipment_model_active(db, actor, model, is_active=False)
    assert model.is_active is False
    # Modelul ramane in DB, gasibil prin id -- doar iesit din search implicit.
    assert svc.get_equipment_model(db, model.id) is not None


def test_search_excludes_inactive_by_default(db):
    manufacturer = make_manufacturer(db)
    active = make_equipment_model(db, manufacturer=manufacturer, model_name="Active Model", equipment_type="battery")
    make_equipment_model(db, manufacturer=manufacturer, model_name="Inactive Model", equipment_type="battery", is_active=False)

    results = svc.search_equipment_models(db, equipment_type="battery")
    assert [m.id for m in results] == [active.id]


def test_search_filters_by_equipment_type(db):
    manufacturer = make_manufacturer(db)
    make_equipment_model(db, manufacturer=manufacturer, model_name="Inv1", equipment_type="inverter")
    battery = make_equipment_model(db, manufacturer=manufacturer, model_name="Bat1", equipment_type="battery")

    results = svc.search_equipment_models(db, equipment_type="battery")
    assert [m.id for m in results] == [battery.id]


def test_search_filters_by_query_text_across_manufacturer_and_model(db):
    deye = make_manufacturer(db, name="Deye")
    other = make_manufacturer(db, name="Other")
    match = make_equipment_model(db, manufacturer=deye, model_name="SUN-10K-SG04LP3", equipment_type="inverter")
    make_equipment_model(db, manufacturer=other, model_name="Unrelated", equipment_type="inverter")

    results = svc.search_equipment_models(db, equipment_type="inverter", query="deye sun-10k")
    assert [m.id for m in results] == [match.id]


def test_build_snapshot_captures_manufacturer_and_specs(db):
    manufacturer = make_manufacturer(db, name="Deye")
    model = make_equipment_model(db, manufacturer=manufacturer, model_name="SUN-10K", specs={"power_kw": 10}, source_note="fisa")
    snapshot = svc.build_snapshot(model)
    assert snapshot == {
        "equipment_model_id": str(model.id),
        "manufacturer": "Deye",
        "model_name": "SUN-10K",
        "equipment_type": "inverter",
        "spec_revision": 1,
        "specs": {"power_kw": 10},
        "source_note": "fisa",
    }
