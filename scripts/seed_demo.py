"""Seed pentru modul demonstrativ: creeaza 2 organizatii/statii demo (una cu
EV, una fara), utilizatori demo si tarife, apoi scrie un fisier JSON cu
coduri de asociere proaspete pentru ca simulatorul (proces separat, vezi
simulator/run.py) sa se poata asocia prin API-ul public.

Ruleaza DOAR daca DEMO_MODE_ENABLED=true (si niciodata in productie, unde
config.py interzice explicit combinatia ENVIRONMENT=production + demo mode).
Este idempotent: poate fi rulat de mai multe ori fara sa duplice organizatii.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.core.security import hash_password
from app.database import SessionLocal
from app.models.organization import Organization
from app.models.station import Station
from app.models.tariff import Tariff, TariffVersion
from app.models.user import User
from app.services import device_service, station_service

settings = get_settings()

DEMO_STATIONS = [
    {
        "key": "demo-fara-ev",
        "org_name": "Familia Ionescu (demo)",
        "station_name": "Casa Ionescu - fara EV",
        "ev_enabled": False,
        "pv_kwp": 6.0,
        "battery_kwh": 10.0,
    },
    {
        "key": "demo-cu-ev",
        "org_name": "Familia Georgescu (demo)",
        "station_name": "Casa Georgescu - cu EV",
        "ev_enabled": True,
        "pv_kwp": 9.0,
        "battery_kwh": 15.0,
    },
]


def get_or_create_demo_admin(db) -> User:
    user = db.scalar(select(User).where(User.email == "demo-admin@ems-platform.local"))
    if user:
        return user
    user = User(
        email="demo-admin@ems-platform.local",
        full_name="Administrator Demo",
        password_hash=hash_password(os.environ.get("DEMO_ADMIN_PASSWORD", "demo-parola-schimba-ma")),
        is_platform_admin=True,
        is_demo=True,
    )
    db.add(user)
    db.flush()
    return user


def main() -> None:
    if not settings.demo_mode_enabled:
        print("DEMO_MODE_ENABLED=false -- seed-ul demo nu ruleaza (comportament asteptat in productie).")
        return

    db = SessionLocal()
    admin = get_or_create_demo_admin(db)
    db.commit()

    seed_entries = []

    for spec in DEMO_STATIONS:
        org = db.scalar(select(Organization).where(Organization.slug == station_service.slugify(spec["org_name"])))
        if org is None:
            org = station_service.create_organization(db, spec["org_name"], admin, is_demo=True)
            db.commit()

        station = db.scalar(select(Station).where(Station.organization_id == org.id, Station.name == spec["station_name"]))
        if station is None:
            station = station_service.create_station(
                db, org,
                name=spec["station_name"], timezone="Europe/Bucharest",
                pv_installed_power_kw=Decimal(str(spec["pv_kwp"])), inverter_power_kw=Decimal(str(spec["pv_kwp"])),
                battery_reference_capacity_kwh=Decimal(str(spec["battery_kwh"])),
                battery_available_capacity_kwh=Decimal(str(spec["battery_kwh"])),
                battery_max_charge_power_kw=Decimal(str(spec["battery_kwh"] * 0.3)),
                battery_max_discharge_power_kw=Decimal(str(spec["battery_kwh"] * 0.3)),
                battery_charge_efficiency=Decimal("0.95"), battery_discharge_efficiency=Decimal("0.95"),
                grid_import_limit_kw=Decimal("10"), grid_export_limit_kw=Decimal("8"),
                ev_enabled=spec["ev_enabled"],
                ev_battery_capacity_kwh=Decimal("55") if spec["ev_enabled"] else None,
                ev_max_charge_power_kw=Decimal("7.4") if spec["ev_enabled"] else None,
                latitude=Decimal("44.4268"), longitude=Decimal("26.1025"),
                created_by=admin, is_demo=True,
            )
            station.is_demo = True
            db.add(station)

            for direction, price in (("import", "0.95"), ("export", "0.35")):
                tariff = Tariff(station_id=station.id, direction=direction, kind="fixed", name=f"Tarif demo {direction}")
                db.add(tariff)
                db.flush()
                db.add(
                    TariffVersion(
                        tariff_id=tariff.id, valid_from=datetime.now(UTC) - timedelta(days=1),
                        fixed_price_lei_per_kwh=Decimal(price), fixed_monthly_fee_lei=Decimal("0"),
                        variable_component_lei_per_kwh=Decimal("0"),
                    )
                )
            db.commit()

        from app.models.device import Device
        from app.models.enums import DeviceStatus

        has_active_device = db.scalar(
            select(Device).where(Device.station_id == station.id, Device.status == DeviceStatus.active.value)
        ) is not None

        raw_code = None
        if not has_active_device:
            _claim, raw_code = device_service.create_claim_code(db, station, admin)
            db.commit()

        seed_entries.append(
            {
                "station_key": spec["key"],
                "claim_code": raw_code,
                "profile": {
                    "timezone": "Europe/Bucharest",
                    "pv_kwp": spec["pv_kwp"],
                    "inverter_kw": spec["pv_kwp"],
                    "battery_capacity_kwh": spec["battery_kwh"],
                    "battery_max_charge_kw": spec["battery_kwh"] * 0.3,
                    "battery_max_discharge_kw": spec["battery_kwh"] * 0.3,
                    "min_reserve_soc": 15.0,
                    "max_normal_soc": 95.0,
                    "allow_grid_charge": False,
                    "allow_battery_export": False,
                    "ev_enabled": spec["ev_enabled"],
                    "ev_battery_kwh": 55.0 if spec["ev_enabled"] else 0.0,
                    "ev_max_charge_kw": 7.4 if spec["ev_enabled"] else 0.0,
                    "seed": abs(hash(spec["key"])) % 100000,
                    "load_base_kw": 0.4,
                    "load_peak_kw": 2.0 if not spec["ev_enabled"] else 2.6,
                },
            }
        )

    db.close()

    seed_file = Path(os.environ.get("SIMULATOR_SEED_FILE", "/data/simulator_seed.json"))
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(json.dumps(seed_entries, indent=2))
    print(f"Seed demo scris in {seed_file} pentru {len(seed_entries)} statii.")


if __name__ == "__main__":
    main()
