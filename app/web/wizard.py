"""Wizard multi-pas pentru configurarea organizatiei/statiei (issue #41).

Design deliberat: NU introducem un tabel de "drafturi" separat si NU
duplicam validarea in template-uri. Wizard-ul e doar un strat subtire de
navigare (progres, Back/Next, breadcrumb) peste rutele/formularele deja
existente si deja testate (`organizations.create_station`,
`stations.station_config_submit/preferences_submit/tariffs_submit/
activate_device_code`) -- fiecare pas ramane complet accesibil si valid si
in afara wizard-ului (`?wizard=1` doar schimba chrome-ul afisat si tinta
redirect-ului dupa succes, nu regulile RBAC/CSRF/validare).

"Draft persistent si reluabil" e satisfacut de faptul ca fiecare pas scrie
imediat in baza de date (acelasi mecanism de versionare/concurenta optimista
deja existent) -- reluarea inseamna doar "recalculeaza pasul urmator din
starea reala salvata", nu restaurarea unui payload de formular nesalvat.
"""
from __future__ import annotations

import uuid
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.models.station import Station
from app.services import station_service

STEP_ORDER: list[str] = ["station", "config", "devices", "tariffs", "preferences", "summary"]

STEP_LABELS: dict[str, str] = {
    "station": "Statie",
    "config": "Echipamente",
    "devices": "Conectare",
    "tariffs": "Tarif",
    "preferences": "Preferinte",
    "summary": "Rezumat",
}

# Pasii pentru care exista deja o valoare implicita rezonabila salvata (fie
# de la crearea statiei, fie pur si simplu optionala) -- pot fi sarite fara
# sa se piarda nimic. "station" nu se poate sari (nu exista inca o statie),
# iar "summary" e ultimul pas (activarea explicita).
SKIPPABLE_STEPS = {"config", "devices", "tariffs", "preferences"}


def _step_url(step: str, station: Station | None, organization_id: uuid.UUID | None) -> str | None:
    if step == "station":
        if organization_id is None:
            return None
        return f"/organizations/{organization_id}/setup/station"
    if station is None:
        return None
    if step == "summary":
        return f"/stations/{station.id}/setup/summary"
    return f"/stations/{station.id}/{step}?{urlencode({'wizard': '1'})}"


def next_wizard_step(progress: dict) -> str | None:
    """Primul pas care mai are sens de parcurs, pornind de la starea reala
    salvata. `None` inseamna ca wizard-ul a fost deja finalizat explicit."""
    if progress["completed"]:
        return None
    if not progress["has_device"]:
        return "devices"
    if not progress["has_tariff"]:
        return "tariffs"
    return "summary"


def resume_url(db: Session, station: Station) -> str:
    progress = station_service.setup_progress(db, station)
    step = next_wizard_step(progress) or "summary"
    url = _step_url(step, station, station.organization_id)
    assert url is not None
    return url


def wizard_chrome_context(db: Session, current_step: str, *, station: Station | None, organization_id: uuid.UUID | None = None) -> dict:
    """Context Jinja pentru `partials/_wizard_progress.html`, inclus in
    fiecare pagina de pas cand vine cu `?wizard=1`."""
    if station is not None:
        organization_id = station.organization_id
        progress = station_service.setup_progress(db, station)
    else:
        progress = {"has_device": False, "has_tariff": False, "completed": False}

    done = {
        "station": station is not None,
        "config": station is not None,
        "devices": progress["has_device"],
        "tariffs": progress["has_tariff"],
        "preferences": station is not None,
        "summary": progress["completed"],
    }

    steps = [
        {
            "key": key,
            "label": STEP_LABELS[key],
            "href": _step_url(key, station, organization_id),
            "done": done[key],
            "current": key == current_step,
        }
        for key in STEP_ORDER
    ]

    idx = STEP_ORDER.index(current_step)
    prev_step = STEP_ORDER[idx - 1] if idx > 0 else None
    next_step = STEP_ORDER[idx + 1] if idx < len(STEP_ORDER) - 1 else None

    if prev_step is not None:
        back_href = _step_url(prev_step, station, organization_id)
        back_label = f"Inapoi: {STEP_LABELS[prev_step]}"
    elif organization_id is not None:
        back_href = f"/organizations/{organization_id}"
        back_label = "Inapoi la organizatie"
    else:
        back_href = None
        back_label = None

    skip_href = None
    if current_step in SKIPPABLE_STEPS and next_step is not None:
        skip_href = _step_url(next_step, station, organization_id)

    return {
        "wizard_step": current_step,
        "wizard_steps": steps,
        "wizard_back_href": back_href,
        "wizard_back_label": back_label,
        "wizard_skip_href": skip_href,
    }
