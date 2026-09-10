from __future__ import annotations

import re
import unicodedata
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.organization import Membership, Organization
from app.models.preference import PreferenceVersion
from app.models.station import PanelGroup, Station, StationConfigVersion
from app.models.user import User


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or uuid.uuid4().hex[:8]


def create_organization(db: Session, name: str, created_by: User, is_demo: bool = False) -> Organization:
    base_slug = slugify(name)
    slug = base_slug
    i = 1
    while db.scalar(select(Organization).where(Organization.slug == slug)) is not None:
        i += 1
        slug = f"{base_slug}-{i}"

    org = Organization(name=name, slug=slug, is_demo=is_demo)
    db.add(org)
    db.flush()

    if not created_by.is_platform_admin:
        db.add(Membership(user_id=created_by.id, organization_id=org.id, role="organization_admin"))
        db.flush()
    return org


def create_station(
    db: Session,
    organization: Organization,
    *,
    name: str,
    timezone: str,
    pv_installed_power_kw: Decimal,
    inverter_power_kw: Decimal,
    battery_reference_capacity_kwh: Decimal | None,
    battery_available_capacity_kwh: Decimal | None,
    battery_max_charge_power_kw: Decimal | None,
    battery_max_discharge_power_kw: Decimal | None,
    battery_charge_efficiency: Decimal | None,
    battery_discharge_efficiency: Decimal | None,
    grid_import_limit_kw: Decimal | None,
    grid_export_limit_kw: Decimal | None,
    ev_enabled: bool,
    ev_battery_capacity_kwh: Decimal | None,
    ev_max_charge_power_kw: Decimal | None,
    latitude: Decimal | None,
    longitude: Decimal | None,
    created_by: User,
    is_demo: bool = False,
    panel_groups: list[dict] | None = None,
) -> Station:
    station = Station(
        organization_id=organization.id,
        name=name,
        timezone=timezone,
        latitude=latitude,
        longitude=longitude,
        is_demo=is_demo,
    )
    db.add(station)
    db.flush()

    config = StationConfigVersion(
        station_id=station.id,
        version=1,
        created_by_user_id=created_by.id,
        pv_installed_power_kw=pv_installed_power_kw,
        inverter_power_kw=inverter_power_kw,
        battery_reference_capacity_kwh=battery_reference_capacity_kwh,
        battery_available_capacity_kwh=battery_available_capacity_kwh or battery_reference_capacity_kwh,
        battery_max_charge_power_kw=battery_max_charge_power_kw,
        battery_max_discharge_power_kw=battery_max_discharge_power_kw,
        battery_charge_efficiency=battery_charge_efficiency or Decimal("0.95"),
        battery_discharge_efficiency=battery_discharge_efficiency or Decimal("0.95"),
        grid_import_limit_kw=grid_import_limit_kw,
        grid_export_limit_kw=grid_export_limit_kw,
        ev_enabled=ev_enabled,
        ev_battery_capacity_kwh=ev_battery_capacity_kwh,
        ev_max_charge_power_kw=ev_max_charge_power_kw,
    )
    db.add(config)
    db.flush()

    groups = panel_groups or [{"name": "Grup principal", "power_kwp": pv_installed_power_kw, "azimuth_degrees": Decimal("180"), "tilt_degrees": Decimal("30")}]
    for g in groups:
        db.add(PanelGroup(station_id=station.id, config_version_id=config.id, **g))

    preference = PreferenceVersion(
        station_id=station.id,
        version=1,
        created_by_user_id=created_by.id,
        min_reserve_soc_percent=Decimal("15"),
        max_normal_soc_percent=Decimal("95"),
    )
    db.add(preference)
    db.flush()

    return station


def next_config_version(db: Session, station: Station) -> int:
    latest = db.scalar(
        select(StationConfigVersion.version)
        .where(StationConfigVersion.station_id == station.id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    return (latest or 0) + 1


def next_preference_version(db: Session, station: Station) -> int:
    latest = db.scalar(
        select(PreferenceVersion.version)
        .where(PreferenceVersion.station_id == station.id)
        .order_by(PreferenceVersion.version.desc())
        .limit(1)
    )
    return (latest or 0) + 1


def detect_preference_conflicts(
    preference: PreferenceVersion, config: StationConfigVersion | None
) -> list[str]:
    """Detectie de conflicte / obiective imposibile intre preferinte si
    constrangerile tehnice/obligatorii. Returneaza explicatii in romana."""
    warnings: list[str] = []

    if preference.min_reserve_soc_percent >= preference.max_normal_soc_percent:
        warnings.append(
            "SOC minim de rezerva este mai mare sau egal cu SOC maxim normal -- "
            "bateria nu ar avea nicio marja de operare intre cele doua limite."
        )

    if config is not None and config.battery_available_capacity_kwh:
        if preference.max_optimization_energy_kwh and preference.max_optimization_energy_kwh > config.battery_available_capacity_kwh:
            warnings.append(
                "Energia maxima autorizata pentru optimizare depaseste capacitatea disponibila a bateriei "
                f"({config.battery_available_capacity_kwh} kWh) -- obiectiv imposibil de atins integral."
            )

    if config is not None and config.ev_enabled and preference.ev_required_energy_kwh and config.ev_battery_capacity_kwh:
        if preference.ev_required_energy_kwh > config.ev_battery_capacity_kwh:
            warnings.append(
                "Energia necesara pentru EV depaseste capacitatea bateriei EV configurate -- "
                "tinta de plecare nu poate fi atinsa integral."
            )

    if preference.max_efc_per_day and preference.max_efc_per_month:
        if preference.max_efc_per_day * 28 > preference.max_efc_per_month:
            warnings.append(
                "Bugetul zilnic de cicluri echivalente (EFC), inmultit cu numarul minim de zile "
                "dintr-o luna, depaseste bugetul lunar -- constrangerile EFC pot intra in conflict."
            )

    if not preference.allow_grid_charge and preference.soc_targets:
        warnings.append(
            "Exista tinte SOC recurente, dar incarcarea din retea este dezactivata -- "
            "tintele pot fi imposibil de atins in zilele cu productie PV insuficienta."
        )

    return warnings
