"""Prognoza de productie PV folosind pvlib pentru pozitia solara si
transpunerea iradiantei pe planul panourilor (POA), plus un model simplu si
documentat de conversie in putere DC/AC.

Model (documentat, nu e un model de modul/invertor certificat CEC/SAPM,
pentru ca nu avem datele de model exacte ale echipamentelor clientului --
doar putere instalata, orientare si inclinatie):
    poa_global = pvlib.irradiance.get_total_irradiance(...)  [W/m2]
    p_dc_kw    = (poa_global / 1000) * putere_kwp * SYSTEM_DERATE
    p_ac_kw    = min(suma(p_dc_kw pe grupuri), putere_invertor_kw)   # clipping AC

SYSTEM_DERATE = 0.85 aproximeaza pierderile tipice (cablare, mismatch,
murdarie, temperatura, eficienta invertor). Este o simplificare documentata,
nu o valoare masurata per instalatie.

Rezolutia prognozei PV mosteneste rezolutia orara a sursei meteo (Open-Meteo);
motorul de optimizare trateaza valoarea ca fiind constanta in cadrul orei
cand construieste grila de 15 minute.

Fereastra de lumina reala (issue #53): orice interval al carui inceput cade
in afara ferestrei [rasarit, apus) REALE a statiei (calculata cu SPA din
latitudine/longitudine, vezi `solar_geometry_service.py`) e clampat explicit
la 0 kW -- indiferent ce valoare bruta de iradianta ar raporta sursa meteo
pentru acel interval (unele surse raporteaza valori mici, nenule, in
amurg/inainte de rasarit, din cauza mediei orare). E o plasa de siguranta
fizica suplimentara, nu inlocuieste modelul pvlib de mai sus (care oricum
converge la 0 aproape de orizont)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd
import pvlib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.forecast import PvForecast, WeatherForecast
from app.models.station import PanelGroup, Station, StationConfigVersion
from app.services.solar_geometry_service import sun_window_for_local_date

SYSTEM_DERATE = 0.85
SOURCE_VERSION_LIMIT = 64


def _latest_config(db: Session, station_id) -> StationConfigVersion | None:
    return db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station_id)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )


def _pv_source_version(weather: WeatherForecast) -> str:
    weather_version = weather.source_version or weather.source
    return f"pvlib={pvlib.__version__};weather={weather_version}"[:SOURCE_VERSION_LIMIT]


def generate_pv_forecast(db: Session, station: Station) -> list[PvForecast]:
    if station.latitude is None or station.longitude is None:
        raise ValueError("Statia nu are coordonate configurate; prognoza PV necesita latitudine/longitudine.")

    config = _latest_config(db, station.id)
    if config is None:
        raise ValueError("Statia nu are configuratie tehnica; prognoza PV necesita cel putin un grup de panouri.")

    panel_groups = db.scalars(
        select(PanelGroup).where(PanelGroup.config_version_id == config.id)
    ).all()
    if not panel_groups:
        raise ValueError("Statia nu are grupuri de panouri configurate.")

    issued_at = db.scalar(
        select(WeatherForecast.issued_at)
        .where(WeatherForecast.station_id == station.id)
        .order_by(WeatherForecast.issued_at.desc())
        .limit(1)
    )
    if issued_at is None:
        raise ValueError("Nu exista prognoza meteo pentru aceasta statie; ruleaza intai importul meteo.")

    weather_rows = db.scalars(
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station.id, WeatherForecast.issued_at == issued_at)
        .order_by(WeatherForecast.interval_start)
    ).all()
    weather_rows = [w for w in weather_rows if w.ghi_w_m2 is not None]
    if not weather_rows:
        raise ValueError("Prognoza meteo nu contine date de iradianta (ghi) utilizabile.")

    times = pd.DatetimeIndex([w.interval_start for w in weather_rows])
    solpos = pvlib.solarposition.get_solarposition(times, float(station.latitude), float(station.longitude))

    total_dc_kw = pd.Series(0.0, index=times)
    for group in panel_groups:
        dni = pd.Series([w.dni_w_m2 or 0.0 for w in weather_rows], index=times)
        ghi = pd.Series([w.ghi_w_m2 or 0.0 for w in weather_rows], index=times)
        dhi = pd.Series([w.dhi_w_m2 or 0.0 for w in weather_rows], index=times)

        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=float(group.tilt_degrees),
            surface_azimuth=float(group.azimuth_degrees),
            solar_zenith=solpos["apparent_zenith"],
            solar_azimuth=solpos["azimuth"],
            dni=dni,
            ghi=ghi,
            dhi=dhi,
        )
        group_dc_kw = (poa["poa_global"].clip(lower=0) / 1000.0) * float(group.power_kwp) * SYSTEM_DERATE
        total_dc_kw = total_dc_kw.add(group_dc_kw, fill_value=0.0)

    inverter_limit = float(config.inverter_power_kw)
    total_ac_kw = total_dc_kw.clip(upper=inverter_limit)

    try:
        tz = ZoneInfo(station.timezone)
    except Exception:
        tz = ZoneInfo("Europe/Bucharest")
    sun_windows: dict = {}
    for ts in times:
        local_date = ts.tz_convert(tz).date() if ts.tzinfo else ts.date()
        if local_date not in sun_windows:
            sun_windows[local_date] = sun_window_for_local_date(
                float(station.latitude), float(station.longitude), tz, local_date
            )

    created = []
    forecast_issued_at = utcnow()
    for i, ts in enumerate(times):
        w = weather_rows[i]
        local_date = ts.tz_convert(tz).date() if ts.tzinfo else ts.date()
        sun_window = sun_windows[local_date]
        power_kw = float(total_ac_kw.iloc[i])
        if not sun_window.is_daylight(w.interval_start):
            power_kw = 0.0
        pv = PvForecast(
            station_id=station.id,
            issued_at=forecast_issued_at,
            interval_start=w.interval_start,
            interval_end=w.interval_end,
            source="pvlib",
            source_version=_pv_source_version(w),
            based_on_weather_forecast_id=w.id,
            predicted_power_kw=round(power_kw, 4),
            scenario="expected",
            confidence=w.confidence,
            is_synthetic=w.is_synthetic,
        )
        db.add(pv)
        created.append(pv)
    db.flush()
    return created
