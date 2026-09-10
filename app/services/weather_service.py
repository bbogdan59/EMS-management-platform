"""Adaptor meteo configurabil. Implementare initiala: Open-Meteo
(https://open-meteo.com), fara autentificare, gratuit pentru uz necomercial.
Cache in Redis (TTL configurabil) ca sa nu interogam API-ul la fiecare
cerere; timeout explicit; indisponibilitatea sursei e propagata clar (nu se
inventeaza date -- prognozele lipsesc explicit din UI/optimizator)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.rate_limit import get_redis
from app.core.security import utcnow
from app.models.forecast import WeatherForecast
from app.models.station import Station

logger = structlog.get_logger(__name__)
settings = get_settings()


class WeatherUnavailableError(Exception):
    pass


HOURLY_VARS = "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,cloud_cover,temperature_2m,wind_speed_10m"


def _cache_key(lat: float, lon: float) -> str:
    return f"weather_forecast:{round(lat, 3)}:{round(lon, 3)}"


def fetch_forecast_raw(latitude: float, longitude: float) -> dict:
    cache = get_redis()
    key = _cache_key(latitude, longitude)
    cached = cache.get(key)
    if cached:
        return json.loads(cached)

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": HOURLY_VARS,
        "timezone": "UTC",
        "forecast_days": 3,
    }
    try:
        with httpx.Client(timeout=settings.weather_request_timeout_seconds) as client:
            resp = client.get(settings.weather_base_url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("weather.fetch_failed", error=str(exc))
        raise WeatherUnavailableError(f"Sursa meteo ({settings.weather_provider}) indisponibila: {exc}") from exc

    cache.setex(key, settings.weather_cache_ttl_minutes * 60, json.dumps(data))
    return data


def store_weather_forecast(db: Session, station: Station, raw: dict) -> list[WeatherForecast]:
    issued_at = utcnow()
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])
    ghi = hourly.get("shortwave_radiation", [])
    dni = hourly.get("direct_normal_irradiance", [])
    dhi = hourly.get("diffuse_radiation", [])
    cloud = hourly.get("cloud_cover", [])
    temp = hourly.get("temperature_2m", [])
    wind = hourly.get("wind_speed_10m", [])

    created = []
    for i, t in enumerate(times):
        interval_start = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        wf = WeatherForecast(
            station_id=station.id,
            issued_at=issued_at,
            interval_start=interval_start,
            interval_end=interval_start + timedelta(hours=1),
            source=settings.weather_provider,
            source_version=raw.get("generationtime_ms") and f"gen_ms={raw['generationtime_ms']}",
            ghi_w_m2=_safe_get(ghi, i),
            dni_w_m2=_safe_get(dni, i),
            dhi_w_m2=_safe_get(dhi, i),
            cloud_cover_percent=_safe_get(cloud, i),
            temperature_c=_safe_get(temp, i),
            wind_speed_ms=_safe_get(wind, i),
        )
        db.add(wf)
        created.append(wf)
    db.flush()
    return created


def _safe_get(arr: list, i: int):
    if i < len(arr) and arr[i] is not None:
        return float(arr[i])
    return None


def refresh_weather_for_station(db: Session, station: Station) -> list[WeatherForecast]:
    if station.latitude is None or station.longitude is None:
        raise WeatherUnavailableError("Statia nu are coordonate (latitudine/longitudine) configurate.")
    raw = fetch_forecast_raw(float(station.latitude), float(station.longitude))
    return store_weather_forecast(db, station, raw)
