from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.models.preference import PreferenceVersion
from app.models.station import StationConfigVersion
from app.services import station_service
from tests.factories import make_org, make_station, make_user


def test_station_config_versioning(db):
    user = make_user(db, email="cfg1@test.local")
    org = make_org(db, "Cfg Org 1")
    station = make_station(db, org, user, name="Cfg Station 1")
    db.commit()

    v1 = station_service.next_config_version(db, station)
    assert v1 == 2  # create_station deja a creat v1

    new_config = StationConfigVersion(
        station_id=station.id, version=v1, pv_installed_power_kw=Decimal("8"), inverter_power_kw=Decimal("8"),
    )
    db.add(new_config)
    db.commit()

    versions = db.scalars(
        select(StationConfigVersion).where(StationConfigVersion.station_id == station.id).order_by(StationConfigVersion.version)
    ).all()
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].pv_installed_power_kw != versions[1].pv_installed_power_kw


def test_preference_conflict_detection_soc_bounds():
    pref = PreferenceVersion(
        station_id=None, version=1, min_reserve_soc_percent=Decimal("90"), max_normal_soc_percent=Decimal("20")
    )
    warnings = station_service.detect_preference_conflicts(pref, None)
    assert any("SOC minim" in w for w in warnings)


def test_preference_conflict_detection_ev_energy_exceeds_capacity():
    from app.models.station import StationConfigVersion as SCV

    config = SCV(
        station_id=None, version=1, pv_installed_power_kw=Decimal("5"), inverter_power_kw=Decimal("5"),
        ev_enabled=True, ev_battery_capacity_kwh=Decimal("40"),
    )
    pref = PreferenceVersion(
        station_id=None, version=1, min_reserve_soc_percent=Decimal("15"), max_normal_soc_percent=Decimal("95"),
        ev_required_energy_kwh=Decimal("60"),
    )
    warnings = station_service.detect_preference_conflicts(pref, config)
    assert any("EV" in w for w in warnings)


def test_preference_conflict_detection_max_energy_exceeds_battery():
    from app.models.station import StationConfigVersion as SCV

    config = SCV(
        station_id=None, version=1, pv_installed_power_kw=Decimal("5"), inverter_power_kw=Decimal("5"),
        battery_available_capacity_kwh=Decimal("10"),
    )
    pref = PreferenceVersion(
        station_id=None, version=1, min_reserve_soc_percent=Decimal("15"), max_normal_soc_percent=Decimal("95"),
        max_optimization_energy_kwh=Decimal("20"),
    )
    warnings = station_service.detect_preference_conflicts(pref, config)
    assert any("bateriei" in w for w in warnings)


def test_no_conflicts_for_sane_preferences():
    from app.models.station import StationConfigVersion as SCV

    config = SCV(
        station_id=None, version=1, pv_installed_power_kw=Decimal("5"), inverter_power_kw=Decimal("5"),
        battery_available_capacity_kwh=Decimal("10"),
    )
    pref = PreferenceVersion(
        station_id=None, version=1, min_reserve_soc_percent=Decimal("15"), max_normal_soc_percent=Decimal("95"),
        max_optimization_energy_kwh=Decimal("5"),
    )
    warnings = station_service.detect_preference_conflicts(pref, config)
    assert warnings == []
