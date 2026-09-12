"""Backoffice pentru catalogul administrabil de echipamente -- issue #42.

Router separat de `admin.py` (care a devenit deja mare) dar montat cu
acelasi prefix `/admin` si aceeasi protectie `require_platform_admin` --
gestionarea catalogului e o capabilitate de platforma, nu per-organizatie.
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, require_platform_admin
from app.core.csrf import verify_csrf
from app.database import get_db
from app.models.enums import EquipmentType
from app.models.equipment_catalog import EquipmentManufacturer, EquipmentModel
from app.models.user import User
from app.services import equipment_catalog_service
from app.services.equipment_catalog_service import EquipmentCatalogError
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_platform_admin)])


def _redirect_with_error(error: str) -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse(f"/admin/catalog?{urlencode({'error': error})}", status_code=303)


@router.get("/catalog")
def catalog_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    manufacturers = db.scalars(
        select(EquipmentManufacturer)
        .options(selectinload(EquipmentManufacturer.models))
        .order_by(EquipmentManufacturer.name)
    ).all()
    context = {
        "manufacturers": manufacturers,
        "equipment_types": [t.value for t in EquipmentType],
        "errors": request.query_params.getlist("error"),
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "admin/catalog.html", context)


@router.post("/catalog/manufacturers", dependencies=[Depends(verify_csrf)])
def create_manufacturer(
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        equipment_catalog_service.create_manufacturer(db, user, name=name)
        db.commit()
    except EquipmentCatalogError as exc:
        db.rollback()
        return _redirect_with_error(str(exc))
    except IntegrityError:
        db.rollback()
        return _redirect_with_error("Un producator cu acest nume exista deja.")
    return RedirectResponse("/admin/catalog", status_code=303)


@router.post("/catalog/manufacturers/{manufacturer_id}/toggle-active", dependencies=[Depends(verify_csrf)])
def toggle_manufacturer_active(
    manufacturer_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    manufacturer = db.get(EquipmentManufacturer, manufacturer_id)
    if manufacturer is None:
        return _redirect_with_error("manufacturer_not_found")
    equipment_catalog_service.set_manufacturer_active(db, user, manufacturer, is_active=not manufacturer.is_active)
    db.commit()
    return RedirectResponse("/admin/catalog", status_code=303)


@router.post("/catalog/models", dependencies=[Depends(verify_csrf)])
def create_equipment_model(
    manufacturer_id: uuid.UUID = Form(...),
    equipment_type: str = Form(...),
    model_name: str = Form(...),
    source_note: str = Form(...),
    specs_json: str = Form("{}"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    manufacturer = db.get(EquipmentManufacturer, manufacturer_id)
    if manufacturer is None:
        return _redirect_with_error("manufacturer_not_found")
    try:
        specs = json.loads(specs_json) if specs_json.strip() else {}
        if not isinstance(specs, dict):
            raise ValueError
    except ValueError:
        return _redirect_with_error("Specificatiile trebuie sa fie un obiect JSON valid.")
    try:
        equipment_catalog_service.create_equipment_model(
            db, user, manufacturer=manufacturer, equipment_type=equipment_type,
            model_name=model_name, specs=specs, source_note=source_note,
        )
        db.commit()
    except EquipmentCatalogError as exc:
        db.rollback()
        return _redirect_with_error(str(exc))
    except IntegrityError:
        db.rollback()
        return _redirect_with_error("Acest model exista deja pentru producatorul si tipul selectat.")
    return RedirectResponse("/admin/catalog", status_code=303)


@router.post("/catalog/models/{model_id}/specs", dependencies=[Depends(verify_csrf)])
def update_equipment_model_specs(
    model_id: uuid.UUID,
    source_note: str = Form(...),
    specs_json: str = Form("{}"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    model = db.get(EquipmentModel, model_id)
    if model is None:
        return _redirect_with_error("model_not_found")
    try:
        specs = json.loads(specs_json) if specs_json.strip() else {}
        if not isinstance(specs, dict):
            raise ValueError
    except ValueError:
        return _redirect_with_error("Specificatiile trebuie sa fie un obiect JSON valid.")
    try:
        equipment_catalog_service.update_equipment_model_specs(db, user, model, specs=specs, source_note=source_note)
        db.commit()
    except EquipmentCatalogError as exc:
        db.rollback()
        return _redirect_with_error(str(exc))
    return RedirectResponse("/admin/catalog", status_code=303)


@router.post("/catalog/models/{model_id}/toggle-active", dependencies=[Depends(verify_csrf)])
def toggle_equipment_model_active(
    model_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    model = db.get(EquipmentModel, model_id)
    if model is None:
        return _redirect_with_error("model_not_found")
    equipment_catalog_service.set_equipment_model_active(db, user, model, is_active=not model.is_active)
    db.commit()
    return RedirectResponse("/admin/catalog", status_code=303)
