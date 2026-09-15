from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.config import get_settings
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.core.rbac import can_manage_station_config, can_modify_operational_settings
from app.core.security import utcnow
from app.database import get_db
from app.models.device import ClaimCode, Device
from app.models.enums import EquipmentType
from app.models.equipment_catalog import EquipmentModel
from app.models.station import PanelGroup, StationConfigVersion
from app.models.tariff import Tariff
from app.models.user import User
from app.schemas.station_forms import PreferenceInput, StationConfigInput
from app.services import device_service, equipment_catalog_service, station_service, tariff_service
from app.web.context import build_nav_context
from app.web.templating import templates
from app.web.wizard import STEP_ORDER, wizard_chrome_context

router = APIRouter()


def _validation_errors(exc: ValidationError) -> list[str]:
    """Mesaje explicite, un rand per eroare -- nu doar json-ul brut Pydantic,
    care ar expune nume de camp tehnice fara context util pentru un
    administrator care completeaza formularul."""
    out = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "formular"
        out.append(f"{field}: {err['msg']}")
    return out


def _none_if_blank(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value


def _error_redirect(path: str, errors: list[str], wizard: bool = False) -> RedirectResponse:
    from urllib.parse import urlencode

    params = [("error", e) for e in errors]
    if wizard:
        params.append(("wizard", "1"))
    query = urlencode(params)
    return RedirectResponse(f"{path}?{query}", status_code=303)


def _config_error_redirect(station_id: uuid.UUID, errors: list[str], wizard: bool = False) -> RedirectResponse:
    return _error_redirect(f"/stations/{station_id}/config", errors, wizard)


def _preferences_error_redirect(station_id: uuid.UUID, errors: list[str], wizard: bool = False) -> RedirectResponse:
    return _error_redirect(f"/stations/{station_id}/preferences", errors, wizard)


def _next_step_redirect(station_id: uuid.UUID, current_step: str) -> RedirectResponse:
    """Dupa un submit reusit in modul wizard, avanseaza la pasul urmator din
    ordinea fixa (issue #41) -- niciodata la un pas calculat din starea reala
    (acela e doar pentru reluare, vezi `next_wizard_step`), ca sa respecte
    navigarea explicita Back/Next parcursa de utilizator."""
    idx = STEP_ORDER.index(current_step)
    next_step = STEP_ORDER[idx + 1] if idx + 1 < len(STEP_ORDER) else "summary"
    if next_step == "summary":
        return RedirectResponse(f"/stations/{station_id}/setup/summary", status_code=303)
    return RedirectResponse(f"/stations/{station_id}/{next_step}?wizard=1", status_code=303)


def _tariff_error_redirect(station_id: uuid.UUID, errors: list[str]) -> RedirectResponse:
    return _error_redirect(f"/stations/{station_id}/tariffs", errors)


def _dec(value: str | None, default: Decimal | None = None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


# --- Configuratie tehnica ---


def _safe_script_json(value) -> str:
    """json.dumps() nu escapeaza `<`/`>`/`&` -- un nume de grup PV salvat anterior
    continand literal `</script>` ar rupe blocul <script> in care e inserat acest
    JSON (cu `| safe` in template) si ar permite XSS stocat. Escapare standard
    pentru JSON inserat in HTML: inlocuim caracterele periculoase cu echivalentul
    lor unicode-escape, valid in JSON si inofensiv pentru parser-ul HTML."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _panel_groups_json(config: StationConfigVersion | None) -> str:
    if config is None:
        return _safe_script_json(
            [{
                "name": "Grup principal", "power_kwp": "0", "azimuth_degrees": "180", "tilt_degrees": "30",
                "pv_module_model_id": None, "pv_module_label": None, "pv_module_count": None,
            }]
        )
    return _safe_script_json(
        [
            {
                "name": g.name, "power_kwp": str(g.power_kwp),
                "azimuth_degrees": str(g.azimuth_degrees), "tilt_degrees": str(g.tilt_degrees),
                "pv_module_model_id": str(g.pv_module_model_id) if g.pv_module_model_id else None,
                "pv_module_label": (
                    f"{g.pv_module_model_snapshot['manufacturer']} {g.pv_module_model_snapshot['model_name']}"
                    if g.pv_module_model_snapshot else g.pv_module_custom_label
                ),
                "pv_module_count": g.pv_module_count,
            }
            for g in config.panel_groups
        ]
    )


def _equipment_selection_context(config: StationConfigVersion | None) -> dict:
    """Contextul minim ca template-ul sa arate selectia curenta (catalog sau
    custom) pentru invertor/baterie, fara sa retrimita formularul."""
    if config is None:
        return {
            "inverter_model_id": None, "inverter_model_label": None, "inverter_custom_label": None,
            "battery_model_id": None, "battery_model_label": None, "battery_custom_label": None,
        }
    return {
        "inverter_model_id": str(config.inverter_model_id) if config.inverter_model_id else None,
        "inverter_model_label": (
            f"{config.inverter_model_snapshot['manufacturer']} {config.inverter_model_snapshot['model_name']}"
            if config.inverter_model_snapshot else None
        ),
        "inverter_custom_label": config.inverter_custom_label,
        "battery_model_id": str(config.battery_model_id) if config.battery_model_id else None,
        "battery_model_label": (
            f"{config.battery_model_snapshot['manufacturer']} {config.battery_model_snapshot['model_name']}"
            if config.battery_model_snapshot else None
        ),
        "battery_custom_label": config.battery_custom_label,
    }


@router.get("/equipment/search")
def equipment_search(
    equipment_type: str,
    q: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Search/autocomplete pentru catalogul de echipamente (issue #42),
    folosit din formularul de configurare a statiei. Orice utilizator
    autentificat poate cauta -- catalogul e metadata comerciala, nu date
    sensibile per-tenant."""
    if equipment_type not in {t.value for t in EquipmentType}:
        return []
    models = equipment_catalog_service.search_equipment_models(db, equipment_type=equipment_type, query=q)
    return [
        {
            "id": str(m.id),
            "manufacturer": m.manufacturer.name,
            "model_name": m.model_name,
            "spec_revision": m.spec_revision,
            "specs": m.specs,
        }
        for m in models
    ]


def _resolve_equipment_model(db: Session, model_id, expected_type: str) -> EquipmentModel:
    model = equipment_catalog_service.get_equipment_model(db, model_id)
    if model is None or model.equipment_type != expected_type:
        raise ValueError("Modelul de echipament selectat nu a fost gasit.")
    return model


@router.get("/stations/{station_id}/config")
def station_config_form(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    station, role = station_role
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    history = db.scalars(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
    ).all()
    context = {
        "station": station,
        "config": config,
        "history": history,
        "panel_groups_json": _panel_groups_json(config),
        "expected_version": config.version if config else 0,
        "can_edit": can_manage_station_config(role),
        "errors": request.query_params.getlist("error"),
        **_equipment_selection_context(config),
        **build_nav_context(db, user, station.id),
    }
    if request.query_params.get("wizard") == "1":
        context.update(wizard_chrome_context(db, "config", station=station))
    return templates.TemplateResponse(request, "stations/config.html", context)


@router.post("/stations/{station_id}/config", dependencies=[Depends(verify_csrf)])
def station_config_submit(
    request: Request,
    pv_installed_power_kw: str = Form(...),
    inverter_power_kw: str = Form(...),
    battery_reference_capacity_kwh: str | None = Form(None),
    battery_available_capacity_kwh: str | None = Form(None),
    battery_max_charge_power_kw: str | None = Form(None),
    battery_max_discharge_power_kw: str | None = Form(None),
    battery_charge_efficiency: str | None = Form(None),
    battery_discharge_efficiency: str | None = Form(None),
    grid_import_limit_kw: str | None = Form(None),
    grid_export_limit_kw: str | None = Form(None),
    ev_enabled: str | None = Form(None),
    ev_battery_capacity_kwh: str | None = Form(None),
    ev_max_charge_power_kw: str | None = Form(None),
    notes: str | None = Form(None),
    panel_groups_json: str = Form(...),
    expected_version: int = Form(...),
    inverter_model_id: str | None = Form(None),
    inverter_custom_label: str | None = Form(None),
    battery_model_id: str | None = Form(None),
    battery_custom_label: str | None = Form(None),
    wizard: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    is_wizard = wizard == "1"

    try:
        panel_groups_raw = json.loads(panel_groups_json)
    except json.JSONDecodeError:
        return _config_error_redirect(station.id, ["Grupurile de panouri: JSON invalid."], is_wizard)
    if not isinstance(panel_groups_raw, list):
        return _config_error_redirect(station.id, ["Grupurile de panouri trebuie sa fie o lista."], is_wizard)

    raw = {
        "pv_installed_power_kw": pv_installed_power_kw,
        "inverter_power_kw": inverter_power_kw,
        "battery_reference_capacity_kwh": _none_if_blank(battery_reference_capacity_kwh),
        "battery_available_capacity_kwh": _none_if_blank(battery_available_capacity_kwh),
        "battery_max_charge_power_kw": _none_if_blank(battery_max_charge_power_kw),
        "battery_max_discharge_power_kw": _none_if_blank(battery_max_discharge_power_kw),
        "grid_import_limit_kw": _none_if_blank(grid_import_limit_kw),
        "grid_export_limit_kw": _none_if_blank(grid_export_limit_kw),
        "ev_enabled": bool(ev_enabled),
        "ev_battery_capacity_kwh": _none_if_blank(ev_battery_capacity_kwh),
        "ev_max_charge_power_kw": _none_if_blank(ev_max_charge_power_kw),
        "notes": notes or None,
        "panel_groups": panel_groups_raw,
        "expected_version": expected_version,
        "inverter_model_id": _none_if_blank(inverter_model_id),
        "inverter_custom_label": _none_if_blank(inverter_custom_label),
        "battery_model_id": _none_if_blank(battery_model_id),
        "battery_custom_label": _none_if_blank(battery_custom_label),
    }
    if battery_charge_efficiency:
        raw["battery_charge_efficiency"] = battery_charge_efficiency
    if battery_discharge_efficiency:
        raw["battery_discharge_efficiency"] = battery_discharge_efficiency

    try:
        validated = StationConfigInput.model_validate(raw)
    except ValidationError as exc:
        return _config_error_redirect(station.id, _validation_errors(exc), is_wizard)

    try:
        inverter_model = (
            _resolve_equipment_model(db, validated.inverter_model_id, EquipmentType.inverter.value)
            if validated.inverter_model_id is not None
            else None
        )
        battery_model = (
            _resolve_equipment_model(db, validated.battery_model_id, EquipmentType.battery.value)
            if validated.battery_model_id is not None
            else None
        )
        pv_models = [
            (
                _resolve_equipment_model(db, group.pv_module_model_id, EquipmentType.pv_module.value)
                if group.pv_module_model_id is not None
                else None
            )
            for group in validated.panel_groups
        ]
    except ValueError as exc:
        return _config_error_redirect(station.id, [str(exc)], is_wizard)

    # Concurenta optimista: daca o alta editare a fost publicata intre randarea
    # formularului si aceasta trimitere, respingem explicit in loc sa suprascriem
    # tacit modificarea celuilalt editor.
    current_version = station_service.next_config_version(db, station) - 1
    if validated.expected_version != current_version:
        return _config_error_redirect(
            station.id,
            [f"Configuratia a fost modificata intre timp (v{current_version} e curenta) -- reincarca pagina si reaplica modificarile."],
            is_wizard,
        )

    version = current_version + 1
    config = StationConfigVersion(
        station_id=station.id,
        version=version,
        created_by_user_id=user.id,
        pv_installed_power_kw=validated.pv_installed_power_kw,
        inverter_power_kw=validated.inverter_power_kw,
        battery_reference_capacity_kwh=validated.battery_reference_capacity_kwh,
        battery_available_capacity_kwh=(
            validated.battery_available_capacity_kwh
            if validated.battery_available_capacity_kwh is not None
            else validated.battery_reference_capacity_kwh
        ),
        battery_max_charge_power_kw=validated.battery_max_charge_power_kw,
        battery_max_discharge_power_kw=validated.battery_max_discharge_power_kw,
        battery_charge_efficiency=validated.battery_charge_efficiency,
        battery_discharge_efficiency=validated.battery_discharge_efficiency,
        grid_import_limit_kw=validated.grid_import_limit_kw,
        grid_export_limit_kw=validated.grid_export_limit_kw,
        ev_enabled=validated.ev_enabled,
        ev_battery_capacity_kwh=validated.ev_battery_capacity_kwh,
        ev_max_charge_power_kw=validated.ev_max_charge_power_kw,
        notes=validated.notes,
        inverter_model_id=inverter_model.id if inverter_model else None,
        inverter_model_spec_revision=inverter_model.spec_revision if inverter_model else None,
        inverter_model_snapshot=equipment_catalog_service.build_snapshot(inverter_model) if inverter_model else None,
        inverter_custom_label=validated.inverter_custom_label,
        battery_model_id=battery_model.id if battery_model else None,
        battery_model_spec_revision=battery_model.spec_revision if battery_model else None,
        battery_model_snapshot=equipment_catalog_service.build_snapshot(battery_model) if battery_model else None,
        battery_custom_label=validated.battery_custom_label,
    )
    db.add(config)
    db.flush()
    for group, pv_model in zip(validated.panel_groups, pv_models, strict=True):
        db.add(
            PanelGroup(
                station_id=station.id,
                config_version_id=config.id,
                name=group.name,
                power_kwp=group.power_kwp,
                azimuth_degrees=group.azimuth_degrees,
                tilt_degrees=group.tilt_degrees,
                pv_module_model_id=pv_model.id if pv_model else None,
                pv_module_model_spec_revision=pv_model.spec_revision if pv_model else None,
                pv_module_model_snapshot=equipment_catalog_service.build_snapshot(pv_model) if pv_model else None,
                pv_module_custom_label=group.pv_module_custom_label,
                pv_module_count=group.pv_module_count,
            )
        )
    record_audit(
        db, action="station_config_updated", resource_type="station_config", resource_id=str(config.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        metadata={"version": version, "panel_group_count": len(validated.panel_groups)},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _config_error_redirect(
            station.id, ["Configuratia a fost modificata concurent -- reincarca pagina si reaplica modificarile."], is_wizard
        )
    if is_wizard:
        return _next_step_redirect(station.id, "config")
    return RedirectResponse(f"/stations/{station.id}/config", status_code=303)


# --- Preferinte ---


def _soc_targets_json(preference) -> str:
    if preference is None or not preference.soc_targets:
        return "[]"
    return _safe_script_json(preference.soc_targets)


@router.get("/stations/{station_id}/preferences")
def preferences_form(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    from app.models.preference import PreferenceVersion

    station, role = station_role
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.station_id == station.id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )
    automation_suspended_until_local = None
    if preference is not None and preference.automation_suspended_until is not None:
        try:
            tz = ZoneInfo(station.timezone)
        except Exception:
            tz = ZoneInfo("Europe/Bucharest")
        automation_suspended_until_local = preference.automation_suspended_until.astimezone(tz)
    context = {
        "station": station,
        "preference": preference,
        "soc_targets_json": _soc_targets_json(preference),
        "automation_suspended_until_local": automation_suspended_until_local,
        "expected_version": preference.version if preference else 0,
        "can_edit": can_modify_operational_settings(role),
        "errors": request.query_params.getlist("error"),
        **build_nav_context(db, user, station.id),
    }
    if request.query_params.get("wizard") == "1":
        context.update(wizard_chrome_context(db, "preferences", station=station))
    return templates.TemplateResponse(request, "stations/preferences.html", context)


@router.post("/stations/{station_id}/preferences", dependencies=[Depends(verify_csrf)])
def preferences_submit(
    request: Request,
    min_reserve_soc_percent: str = Form(...),
    max_normal_soc_percent: str = Form(...),
    max_optimization_energy_kwh: str | None = Form(None),
    allow_grid_charge: str | None = Form(None),
    allow_battery_export: str | None = Form(None),
    max_efc_per_day: str | None = Form(None),
    max_efc_per_month: str | None = Form(None),
    priority: str = Form("cost"),
    soc_targets_json: str = Form("[]"),
    ev_required_energy_kwh: str | None = Form(None),
    ev_departure_time: str | None = Form(None),
    automation_suspended_until: str | None = Form(None),
    arbitrage_min_benefit_lei: str = Form("0"),
    expected_version: int = Form(...),
    wizard: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="operator")),
    user: User = Depends(get_current_user),
):
    from app.models.preference import PreferenceVersion

    station, _role = station_role
    is_wizard = wizard == "1"

    try:
        soc_targets_raw = json.loads(soc_targets_json) if soc_targets_json else []
    except json.JSONDecodeError:
        return _preferences_error_redirect(station.id, ["Tintele SOC: JSON invalid."], is_wizard)
    if not isinstance(soc_targets_raw, list):
        return _preferences_error_redirect(station.id, ["Tintele SOC trebuie sa fie o lista."], is_wizard)

    ev_time_str = None
    if ev_departure_time:
        try:
            h, m = ev_departure_time.split(":")
            ev_time_str = f"{int(h):02d}:{int(m):02d}"
        except ValueError:
            return _preferences_error_redirect(station.id, ["Ora plecare EV: format invalid (asteptat HH:MM)."], is_wizard)

    raw = {
        "min_reserve_soc_percent": min_reserve_soc_percent,
        "max_normal_soc_percent": max_normal_soc_percent,
        "max_optimization_energy_kwh": _none_if_blank(max_optimization_energy_kwh),
        "allow_grid_charge": bool(allow_grid_charge),
        "allow_battery_export": bool(allow_battery_export),
        "max_efc_per_day": _none_if_blank(max_efc_per_day),
        "max_efc_per_month": _none_if_blank(max_efc_per_month),
        "priority": priority,
        "soc_targets": soc_targets_raw,
        "ev_required_energy_kwh": _none_if_blank(ev_required_energy_kwh),
        "automation_suspended_until": _none_if_blank(automation_suspended_until),
        "arbitrage_min_benefit_lei": arbitrage_min_benefit_lei or "0",
        "expected_version": expected_version,
    }
    if ev_time_str:
        raw["ev_departure_time"] = ev_time_str

    try:
        validated = PreferenceInput.model_validate(raw)
    except ValidationError as exc:
        return _preferences_error_redirect(station.id, _validation_errors(exc), is_wizard)

    suspended_until_utc = None
    if validated.automation_suspended_until:
        try:
            tz = ZoneInfo(station.timezone)
        except Exception:
            tz = ZoneInfo("Europe/Bucharest")
        try:
            local_naive = datetime.fromisoformat(validated.automation_suspended_until)
        except ValueError:
            return _preferences_error_redirect(station.id, ["Suspendare automatizare: data/ora invalida."], is_wizard)
        candidates = [local_naive.replace(tzinfo=tz, fold=fold) for fold in (0, 1)]
        valid = [
            candidate
            for candidate in candidates
            if candidate.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == local_naive
        ]
        if not valid or valid[0].utcoffset() != valid[-1].utcoffset():
            return _preferences_error_redirect(
                station.id,
                ["Suspendare automatizare: ora locala este inexistenta sau ambigua din cauza schimbarii DST."],
                is_wizard,
            )
        suspended_until_utc = valid[0].astimezone(UTC)

    current_version = station_service.next_preference_version(db, station) - 1
    if validated.expected_version != current_version:
        return _preferences_error_redirect(
            station.id,
            [f"Preferintele au fost modificate intre timp (v{current_version} e curenta) -- reincarca pagina si reaplica modificarile."],
            is_wizard,
        )
    version = current_version + 1

    preference = PreferenceVersion(
        station_id=station.id,
        version=version,
        created_by_user_id=user.id,
        min_reserve_soc_percent=validated.min_reserve_soc_percent,
        max_normal_soc_percent=validated.max_normal_soc_percent,
        max_optimization_energy_kwh=validated.max_optimization_energy_kwh,
        allow_grid_charge=validated.allow_grid_charge,
        allow_battery_export=validated.allow_battery_export,
        max_efc_per_day=validated.max_efc_per_day,
        max_efc_per_month=validated.max_efc_per_month,
        priority=validated.priority,
        soc_targets=[t.model_dump(mode="json") for t in validated.soc_targets],
        ev_required_energy_kwh=validated.ev_required_energy_kwh,
        ev_departure_time=validated.ev_departure_time,
        automation_suspended_until=suspended_until_utc,
        arbitrage_min_benefit_lei=validated.arbitrage_min_benefit_lei,
    )

    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    preference.conflict_warnings = station_service.detect_preference_conflicts(preference, config)

    db.add(preference)
    record_audit(
        db, action="preferences_updated", resource_type="preference", resource_id=str(station.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        metadata={"version": version, "conflicts": preference.conflict_warnings},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _preferences_error_redirect(
            station.id, ["Preferintele au fost modificate concurent -- reincarca pagina si reaplica modificarile."], is_wizard
        )
    if is_wizard:
        return _next_step_redirect(station.id, "preferences")
    return RedirectResponse(f"/stations/{station.id}/preferences", status_code=303)


# --- Tarife ---


_INVOICE_PREVIEW_SAMPLE_KWH = Decimal(300)


@router.get("/stations/{station_id}/tariffs")
def tariffs_page(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    from app.models.market import MarketPriceInterval

    station, role = station_role
    tariffs = db.scalars(select(Tariff).where(Tariff.station_id == station.id)).all()

    latest_market_price = db.scalar(
        select(MarketPriceInterval.price_lei_per_kwh)
        .where(MarketPriceInterval.source == "opcom_pzu", MarketPriceInterval.is_current.is_(True))
        .order_by(MarketPriceInterval.interval_start.desc())
        .limit(1)
    )

    previews: dict[uuid.UUID, dict] = {}
    for tariff in tariffs:
        latest_version = max(tariff.versions, key=lambda v: v.valid_from, default=None)
        if latest_version is not None:
            previews[latest_version.id] = tariff_service.build_invoice_preview(
                latest_version, latest_market_price, _INVOICE_PREVIEW_SAMPLE_KWH
            )

    context = {
        "station": station,
        "tariffs": tariffs,
        "previews": previews,
        "preview_sample_kwh": _INVOICE_PREVIEW_SAMPLE_KWH,
        "can_edit": can_manage_station_config(role),
        "errors": request.query_params.getlist("error"),
        **build_nav_context(db, user, station.id),
    }
    if request.query_params.get("wizard") == "1":
        context.update(wizard_chrome_context(db, "tariffs", station=station))
    return templates.TemplateResponse(request, "stations/tariffs.html", context)


@router.post("/stations/{station_id}/tariffs", dependencies=[Depends(verify_csrf)])
def tariffs_submit(
    request: Request,
    direction: str = Form(...),
    kind: str = Form(...),
    name: str = Form(...),
    fixed_price_lei_per_kwh: str | None = Form(None),
    opcom_margin_lei_per_kwh: str | None = Form(None),
    fixed_monthly_fee_lei: str = Form("0"),
    variable_component_lei_per_kwh: str = Form("0"),
    distribution_lei_per_kwh: str = Form("0"),
    transport_lei_per_kwh: str = Form("0"),
    other_regulated_lei_per_kwh: str = Form("0"),
    vat_rate_percent: str | None = Form(None),
    settlement_method: str = Form("net_metering_15min"),
    settlement_interval_days: int = Form(30),
    economic_calculation_disabled: str | None = Form(None),
    limitation_note: str | None = Form(None),
    wizard: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    try:
        tariff = tariff_service.get_or_create_tariff(db, station, direction, kind, name)
        tariff_service.add_tariff_version(
            db,
            tariff,
            valid_from=datetime.now(UTC),
            fixed_price_lei_per_kwh=_dec(fixed_price_lei_per_kwh),
            opcom_margin_lei_per_kwh=_dec(opcom_margin_lei_per_kwh),
            fixed_monthly_fee_lei=_dec(fixed_monthly_fee_lei, Decimal("0")),
            variable_component_lei_per_kwh=_dec(variable_component_lei_per_kwh, Decimal("0")),
            distribution_lei_per_kwh=_dec(distribution_lei_per_kwh, Decimal("0")),
            transport_lei_per_kwh=_dec(transport_lei_per_kwh, Decimal("0")),
            other_regulated_lei_per_kwh=_dec(other_regulated_lei_per_kwh, Decimal("0")),
            vat_rate_percent=_dec(vat_rate_percent),
            settlement_method=settlement_method,
            settlement_interval_days=settlement_interval_days,
            economic_calculation_disabled=bool(economic_calculation_disabled),
            limitation_note=limitation_note,
        )
    except ValueError as exc:
        db.rollback()
        return _tariff_error_redirect(station.id, [str(exc)])
    record_audit(
        db, action="tariff_version_created", resource_type="tariff", resource_id=str(tariff.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
    )
    db.commit()
    if wizard == "1":
        return _next_step_redirect(station.id, "tariffs")
    return RedirectResponse(f"/stations/{station.id}/tariffs", status_code=303)


# --- Dispozitive / coduri de asociere ---


@router.get("/stations/{station_id}/devices")
def devices_page(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="operator")),
    user: User = Depends(get_current_user),
):
    station, role = station_role
    devices = db.scalars(select(Device).where(Device.station_id == station.id)).all()
    claim_codes = db.scalars(
        select(ClaimCode).where(ClaimCode.station_id == station.id).order_by(ClaimCode.created_at.desc()).limit(10)
    ).all()
    context = {
        "station": station,
        "devices": devices,
        "claim_codes": claim_codes,
        "can_edit": can_manage_station_config(role),
        "new_claim_code": None,
        "legacy_claim_code_enabled": get_settings().legacy_claim_code_enabled,
        "onboarding": request.query_params.get("onboarding") == "1",
        "now": utcnow(),
        **build_nav_context(db, user, station.id),
    }
    if request.query_params.get("wizard") == "1":
        context.update(wizard_chrome_context(db, "devices", station=station))
    return templates.TemplateResponse(request, "stations/devices.html", context)


@router.post("/stations/{station_id}/devices/activate", dependencies=[Depends(verify_csrf)])
def activate_device_code(
    request: Request,
    activation_code: str = Form(...),
    wizard: str | None = Form(None),
    onboarding: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    is_wizard = wizard == "1"
    # Pastreaza pasul din wizard-ul de setare (issue #44) prin redirect, ca
    # utilizatorul sa ajunga inapoi in fluxul "statie -> asociere device ->
    # configurare" in loc sa cada pe pagina simpla de dispozitive.
    wizard_qs = "&wizard=1" if is_wizard else ("&onboarding=1" if onboarding == "1" else "")
    try:
        check_fixed_window(
            f"device_activation:{user.id}:{station.id}",
            get_settings().device_activation_attempts_per_hour,
            3600,
        )
    except RateLimitExceeded:
        return RedirectResponse(f"/stations/{station.id}/devices?error=too_many_attempts{wizard_qs}", status_code=303)

    try:
        device = device_service.activate_device_for_station(db, activation_code, station, user)
    except device_service.DeviceServiceError:
        db.rollback()
        return RedirectResponse(f"/stations/{station.id}/devices?error=invalid_device_code{wizard_qs}", status_code=303)

    record_audit(
        db, action="device_activated_by_customer", resource_type="device", resource_id=str(device.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        metadata={"serial_number": device.serial_number},
    )
    db.commit()
    if is_wizard:
        return _next_step_redirect(station.id, "devices")
    return RedirectResponse(f"/stations/{station.id}/devices?linked=1{wizard_qs}", status_code=303)


@router.post("/stations/{station_id}/claim-codes", dependencies=[Depends(verify_csrf)])
def create_claim_code(
    request: Request,
    wizard: str | None = Form(None),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    if not get_settings().legacy_claim_code_enabled:
        # Fluxul legacy e dezactivat explicit (issue #44) -- refuzam cererea in
        # loc sa generam tacit un cod care ar redeveni un bypass fata de
        # Device Code-ul sigilat (dovada de posesie reala).
        return RedirectResponse(f"/stations/{station.id}/devices?error=legacy_disabled", status_code=303)
    claim, raw_code = device_service.create_claim_code(db, station, user)
    record_audit(
        db, action="claim_code_created", resource_type="claim_code", resource_id=str(claim.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id,
    )
    db.commit()
    devices = db.scalars(select(Device).where(Device.station_id == station.id)).all()
    claim_codes = db.scalars(
        select(ClaimCode).where(ClaimCode.station_id == station.id).order_by(ClaimCode.created_at.desc()).limit(10)
    ).all()
    extra_context = wizard_chrome_context(db, "devices", station=station) if wizard == "1" else {}
    response = templates.TemplateResponse(
        request,
        "stations/devices.html",
        {
            "station": station,
            "devices": devices,
            "claim_codes": claim_codes,
            "can_edit": True,
            "new_claim_code": raw_code,
            "legacy_claim_code_enabled": True,
            "onboarding": request.query_params.get("onboarding") == "1",
            "now": utcnow(),
            **extra_context,
            **build_nav_context(db, user, station.id),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/stations/{station_id}/devices/{device_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke_device(
    request: Request,
    station_id: uuid.UUID,
    device_id: uuid.UUID,
    reason: str = Form("Revocat manual din UI"),
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    station, _role = station_role
    device = db.get(Device, device_id)
    if device is None or device.station_id != station.id:
        return RedirectResponse(f"/stations/{station.id}/devices", status_code=303)
    device_service.revoke_device(db, device, reason)
    record_audit(
        db, action="device_revoked", resource_type="device", resource_id=str(device.id),
        actor_user_id=user.id, actor_label=user.email, station_id=station.id, metadata={"reason": reason},
    )
    db.commit()
    return RedirectResponse(f"/stations/{station.id}/devices", status_code=303)


# --- Rezumat wizard si activare (issue #41) ---


@router.get("/stations/{station_id}/setup/summary")
def setup_summary(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="viewer")),
    user: User = Depends(get_current_user),
):
    from app.models.preference import PreferenceVersion

    station, role = station_role
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.station_id == station.id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )
    devices = db.scalars(select(Device).where(Device.station_id == station.id, Device.status == "active")).all()
    tariffs = db.scalars(select(Tariff).where(Tariff.station_id == station.id)).all()
    context = {
        "station": station,
        "config": config,
        "preference": preference,
        "devices": devices,
        "tariffs": tariffs,
        "can_activate": can_manage_station_config(role),
        **wizard_chrome_context(db, "summary", station=station),
        **build_nav_context(db, user, station.id),
    }
    return templates.TemplateResponse(request, "stations/setup_summary.html", context)


@router.post("/stations/{station_id}/setup/activate", dependencies=[Depends(verify_csrf)])
def setup_activate(
    request: Request,
    db: Session = Depends(get_db),
    station_role: tuple = Depends(StationAccess(min_role="organization_admin")),
    user: User = Depends(get_current_user),
):
    """Pasul final al wizard-ului (issue #41). Idempotent: un al doilea
    submit nu modifica nimic in plus -- pastreaza momentul PRIMEI activari,
    nu-l suprascrie -- si redirecteaza oricum direct in dashboard-ul statiei,
    exact ca prima data (criteriul de acceptare: "dupa finalizare, utilizatorul
    ajunge direct in dashboard-ul statiei")."""
    station, _role = station_role
    if station.setup_completed_at is None:
        station.setup_completed_at = datetime.now(UTC)
        record_audit(
            db, action="station_setup_completed", resource_type="station", resource_id=str(station.id),
            actor_user_id=user.id, actor_label=user.email, station_id=station.id,
        )
        db.commit()
    return RedirectResponse(f"/?station_id={station.id}", status_code=303)
