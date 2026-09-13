from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.forecast import WeatherForecast
from app.services import pv_forecast_service
from app.services.solar_geometry_service import compute_sun_window
from tests.factories import make_org, make_station, make_user


def _station(db, suffix=""):
    user = make_user(db, email=f"pvfc{suffix}@test.local")
    org = make_org(db, f"PVFC Org {suffix}")
    # Bucuresti (lat/lon implicite din factory) -- StationConfigVersion + un
    # PanelGroup ("Grup principal", 5 kWp) sunt create automat de create_station.
    return make_station(db, org, user, name=f"PVFC Station {suffix}")


def _add_weather(db, station, issued_at, interval_start, *, ghi):
    db.add(
        WeatherForecast(
            station_id=station.id,
            issued_at=issued_at,
            interval_start=interval_start,
            interval_end=interval_start + timedelta(hours=1),
            source="test",
            ghi_w_m2=ghi,
            dni_w_m2=ghi,
            dhi_w_m2=0.0,
            cloud_cover_percent=0.0,
            temperature_c=20.0,
            wind_speed_ms=1.0,
        )
    )


def test_generate_pv_forecast_clamps_power_outside_real_daylight_window(db):
    """O sursa meteo poate raporta o valoare mica, nenula, de iradianta chiar
    inainte de rasarit/dupa apus (medie orara) -- prognoza PV nu trebuie sa
    produca putere pozitiva pentru un interval clar nocturn, indiferent de
    acea valoare bruta. Verificat pe o zi de vara (21 iunie 2026), unde
    fereastra reala [rasarit, apus) e cunoscuta explicit prin
    `solar_geometry_service`, nu presupusa."""
    station = _station(db, "night")
    sun_window = compute_sun_window(float(station.latitude), float(station.longitude), datetime(2026, 6, 21).date())

    clearly_night = sun_window.sunrise_utc - timedelta(hours=2)
    clearly_day = sun_window.sunrise_utc + timedelta(hours=4)

    issued_at = clearly_night - timedelta(hours=1)
    _add_weather(db, station, issued_at, clearly_night, ghi=50.0)  # artefact de senzor/sursa, nu ar trebui sa produca putere
    _add_weather(db, station, issued_at, clearly_day, ghi=700.0)
    db.commit()

    created = pv_forecast_service.generate_pv_forecast(db, station)
    db.commit()

    by_start = {pv.interval_start: pv for pv in created}
    assert float(by_start[clearly_night].predicted_power_kw) == 0.0
    assert float(by_start[clearly_day].predicted_power_kw) > 0.0


def test_generate_pv_forecast_still_zero_at_night_without_daylight_clamp_artifact():
    """Control: pentru un GHI real de noapte (0), rezultatul e oricum 0 --
    clamp-ul explicit de mai sus e o plasa de siguranta suplimentara pentru
    artefacte ale sursei meteo, nu singura cale prin care noaptea produce 0."""
    sun_window = compute_sun_window(44.43, 26.10, datetime(2026, 6, 21).date())
    clearly_night = sun_window.sunset_utc + timedelta(hours=3)
    assert sun_window.is_daylight(clearly_night) is False


def test_generate_pv_forecast_raises_without_weather(db):
    station = _station(db, "nowx")

    with pytest.raises(ValueError):
        pv_forecast_service.generate_pv_forecast(db, station)
