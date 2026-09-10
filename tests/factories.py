from __future__ import annotations

from decimal import Decimal

from app.core.security import hash_password
from app.models.organization import Membership, Organization
from app.models.user import User
from app.services import station_service


def make_user(db, email="user@test.local", password="TestPass1234", is_platform_admin=False, is_active=True) -> User:
    user = User(
        email=email, full_name="Test User", password_hash=hash_password(password),
        is_platform_admin=is_platform_admin, is_active=is_active,
    )
    db.add(user)
    db.flush()
    return user


def make_org(db, name="Test Org") -> Organization:
    org = Organization(name=name, slug=station_service.slugify(name))
    db.add(org)
    db.flush()
    return org


def make_membership(db, user, org, role="operator") -> Membership:
    m = Membership(user_id=user.id, organization_id=org.id, role=role)
    db.add(m)
    db.flush()
    return m


def make_device(db, station, name="Test Device"):
    from app.models.device import Device
    from app.models.enums import DeviceStatus

    device = Device(station_id=station.id, name=name, status=DeviceStatus.active.value)
    db.add(device)
    db.flush()
    return device


def make_station(db, org, user, name="Test Station", ev_enabled=False, **overrides):
    defaults = dict(
        name=name, timezone="Europe/Bucharest",
        pv_installed_power_kw=Decimal("5"), inverter_power_kw=Decimal("5"),
        battery_reference_capacity_kwh=Decimal("10"), battery_available_capacity_kwh=Decimal("10"),
        battery_max_charge_power_kw=Decimal("3"), battery_max_discharge_power_kw=Decimal("3"),
        battery_charge_efficiency=Decimal("0.95"), battery_discharge_efficiency=Decimal("0.95"),
        grid_import_limit_kw=Decimal("10"), grid_export_limit_kw=Decimal("10"),
        ev_enabled=ev_enabled, ev_battery_capacity_kwh=Decimal("50") if ev_enabled else None,
        ev_max_charge_power_kw=Decimal("7") if ev_enabled else None,
        latitude=Decimal("44.43"), longitude=Decimal("26.10"), created_by=user,
    )
    defaults.update(overrides)
    return station_service.create_station(db, org, **defaults)
