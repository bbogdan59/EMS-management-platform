"""Catalog administrabil de echipamente (invertoare/baterii/panouri) -- issue #42.

Utilizatorul selecteaza un `EquipmentModel` dintr-un search/autocomplete
(`search_equipment_models`), iar platforma precompleteaza parametrii
cunoscuti dintr-un snapshot (`build_snapshot`) capturat la momentul
selectiei -- o editare ulterioara a specificatiilor unui model
(`update_equipment_model_specs`, care creste `spec_revision`) NU modifica
retroactiv configuratiile deja publicate care il refera.

Un model dezactivat (`set_manufacturer_active`/`set_equipment_model_active`
cu `is_active=False`) e nedistructiv: ramane vizibil in istoricul
configuratiilor care il refera (prin snapshot, nu prin lookup live), dar
dispare din `search_equipment_models` (folosit la configurarea de statii
NOI). Nu exista hard-delete -- un model gresit introdus se dezactiveaza,
nu se sterge, ca sa nu rupa `EquipmentModel.id` deja referite.

Catalogul NU contine implicit harti de registre RS485: a sti ca un invertor
e "Deye SUN-10K-SG04LP3" nu inseamna ca stim cum sa ii controlam bateria
prin Modbus (`InverterProfile`, issue #17, ramane un artefact separat,
aprobat explicit).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.models.enums import EquipmentType
from app.models.equipment_catalog import EquipmentManufacturer, EquipmentModel
from app.models.user import User


class EquipmentCatalogError(Exception):
    pass


def _clean(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise EquipmentCatalogError("Campul este obligatoriu.")
    return value


def create_manufacturer(db: Session, actor: User, *, name: str) -> EquipmentManufacturer:
    manufacturer = EquipmentManufacturer(name=_clean(name), is_active=True)
    db.add(manufacturer)
    db.flush()
    record_audit(
        db, action="equipment_manufacturer_created", resource_type="equipment_manufacturer",
        resource_id=str(manufacturer.id), actor_user_id=actor.id, actor_label=actor.email,
        metadata={"name": manufacturer.name},
    )
    return manufacturer


def set_manufacturer_active(db: Session, actor: User, manufacturer: EquipmentManufacturer, *, is_active: bool) -> EquipmentManufacturer:
    manufacturer.is_active = is_active
    db.add(manufacturer)
    db.flush()
    record_audit(
        db, action="equipment_manufacturer_activated" if is_active else "equipment_manufacturer_deactivated",
        resource_type="equipment_manufacturer", resource_id=str(manufacturer.id),
        actor_user_id=actor.id, actor_label=actor.email,
    )
    return manufacturer


def create_equipment_model(
    db: Session, actor: User, *,
    manufacturer: EquipmentManufacturer, equipment_type: str, model_name: str,
    specs: dict, source_note: str,
) -> EquipmentModel:
    if equipment_type not in {t.value for t in EquipmentType}:
        raise EquipmentCatalogError(f"Tip de echipament necunoscut: '{equipment_type}'.")
    model = EquipmentModel(
        manufacturer_id=manufacturer.id,
        equipment_type=equipment_type,
        model_name=_clean(model_name),
        is_active=True,
        spec_revision=1,
        specs=specs or {},
        source_note=_clean(source_note),
        created_by_user_id=actor.id,
    )
    db.add(model)
    db.flush()
    record_audit(
        db, action="equipment_model_created", resource_type="equipment_model", resource_id=str(model.id),
        actor_user_id=actor.id, actor_label=actor.email,
        metadata={"manufacturer": manufacturer.name, "equipment_type": equipment_type, "model_name": model.model_name},
    )
    return model


def update_equipment_model_specs(db: Session, actor: User, model: EquipmentModel, *, specs: dict, source_note: str) -> EquipmentModel:
    """Publica o noua revizie de specificatii. Configuratiile deja publicate
    care refera acest model isi pastreaza propriul snapshot (imutabil) --
    doar selectiile VIITOARE vad noile valori."""
    before_revision = model.spec_revision
    model.specs = specs or {}
    model.source_note = _clean(source_note)
    model.spec_revision = before_revision + 1
    db.add(model)
    db.flush()
    record_audit(
        db, action="equipment_model_specs_updated", resource_type="equipment_model", resource_id=str(model.id),
        actor_user_id=actor.id, actor_label=actor.email,
        metadata={"from_spec_revision": before_revision, "to_spec_revision": model.spec_revision},
    )
    return model


def set_equipment_model_active(db: Session, actor: User, model: EquipmentModel, *, is_active: bool) -> EquipmentModel:
    model.is_active = is_active
    db.add(model)
    db.flush()
    record_audit(
        db, action="equipment_model_activated" if is_active else "equipment_model_deactivated",
        resource_type="equipment_model", resource_id=str(model.id),
        actor_user_id=actor.id, actor_label=actor.email,
    )
    return model


def search_equipment_models(
    db: Session, *, equipment_type: str, query: str | None = None, only_active: bool = True, limit: int = 20,
) -> list[EquipmentModel]:
    stmt = select(EquipmentModel).where(EquipmentModel.equipment_type == equipment_type)
    if only_active:
        stmt = stmt.where(EquipmentModel.is_active.is_(True))
    if query and query.strip():
        needle = f"%{query.strip().lower()}%"
        stmt = (
            stmt.join(EquipmentManufacturer, EquipmentModel.manufacturer_id == EquipmentManufacturer.id)
            .where(func.lower(EquipmentManufacturer.name + " " + EquipmentModel.model_name).like(needle))
        )
    stmt = stmt.order_by(EquipmentModel.model_name).limit(min(limit, 50))
    return list(db.scalars(stmt).all())


def get_equipment_model(db: Session, model_id: uuid.UUID) -> EquipmentModel | None:
    return db.get(EquipmentModel, model_id)


def build_snapshot(model: EquipmentModel) -> dict:
    """Snapshot imutabil de retinut pe consumator (`StationConfigVersion`/
    `PanelGroup`) la momentul selectiei -- nu un pointer live catre catalog."""
    return {
        "equipment_model_id": str(model.id),
        "manufacturer": model.manufacturer.name,
        "model_name": model.model_name,
        "equipment_type": model.equipment_type,
        "spec_revision": model.spec_revision,
        "specs": dict(model.specs or {}),
        "source_note": model.source_note,
    }
