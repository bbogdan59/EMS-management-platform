"""Adaptor meteo configurabil. Implementare initiala: Open-Meteo
(https://open-meteo.com), fara autentificare, gratuit pentru uz necomercial.
Cache in Redis (TTL configurabil) ca sa nu interogam API-ul la fiecare
cerere; timeout explicit; indisponibilitatea sursei e propagata clar (nu se
inventeaza date -- prognozele lipsesc explicit din UI/optimizator)."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

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


HOURLY_VARS = "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,cloud_cover,temperature_2m,precipitation,wind_speed_10m"
REQUIRED_HOURLY_SERIES = ("time", "shortwave_radiation", "cloud_cover", "temperature_2m")
OPTIONAL_HOURLY_SERIES = ("direct_normal_irradiance", "diffuse_radiation", "precipitation", "wind_speed_10m")
ALLOWED_CONFIDENCE = {"nominal", "low", "high"}


@dataclass(frozen=True)
class WeatherProviderRequest:
    latitude: float
    longitude: float
    base_url: str
    timeout_seconds: float
    cache_ttl_minutes: int
    max_retries: int = 3
    retry_backoff_seconds: float = 0.2
    rate_limit_per_minute: int | None = None


class WeatherProvider(Protocol):
    name: str

    def fetch_raw(self, request: WeatherProviderRequest) -> dict:
        """Returneaza payload-ul brut al providerului, cu timestamps UTC."""


class OpenMeteoWeatherProvider:
    name = "open-meteo"

    def fetch_raw(self, request: WeatherProviderRequest) -> dict:
        cache = get_redis()
        key = _cache_key(self.name, request.latitude, request.longitude)
        cached = cache.get(key)
        if cached:
            return json.loads(cached)
        _check_provider_rate_limit(cache, self.name, request.rate_limit_per_minute)

        params = {
            "latitude": request.latitude,
            "longitude": request.longitude,
            "hourly": HOURLY_VARS,
            "timezone": "UTC",
            "forecast_days": 3,
        }
        max_retries = max(1, request.max_retries)
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, max_retries + 1):
            try:
                with httpx.Client(timeout=request.timeout_seconds) as client:
                    resp = client.get(request.base_url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    break
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("weather.fetch_failed", provider=self.name, attempt=attempt, max_retries=max_retries, error=str(exc))
                if attempt < max_retries:
                    time.sleep(request.retry_backoff_seconds * (2 ** (attempt - 1)))
        else:
            assert last_error is not None
            raise WeatherUnavailableError(f"Sursa meteo ({self.name}) indisponibila: {last_error}") from last_error

        cache.setex(key, request.cache_ttl_minutes * 60, json.dumps(data))
        return data


PROVIDERS: dict[str, WeatherProvider] = {
    OpenMeteoWeatherProvider.name: OpenMeteoWeatherProvider(),
}


def _cache_key(provider: str, lat: float, lon: float) -> str:
    return f"weather_forecast:{provider}:{round(lat, 3)}:{round(lon, 3)}"


def _check_provider_rate_limit(cache, provider: str, limit_per_minute: int | None) -> None:
    if limit_per_minute is None or limit_per_minute <= 0:
        return
    bucket = utcnow().strftime("%Y%m%d%H%M")
    key = f"weather_rate:{provider}:{bucket}"
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 90)
    if count > limit_per_minute:
        raise WeatherUnavailableError(f"Limita locala de apeluri meteo depasita pentru {provider}: {limit_per_minute}/minut.")


def get_weather_provider(name: str | None = None) -> WeatherProvider:
    provider_name = name or settings.weather_provider
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise WeatherUnavailableError(f"Provider meteo neacceptat: {provider_name}")
    return provider


def fetch_forecast_raw(latitude: float, longitude: float, provider_name: str | None = None) -> dict:
    provider = get_weather_provider(provider_name)
    request = WeatherProviderRequest(
        latitude=latitude,
        longitude=longitude,
        base_url=settings.weather_base_url,
        timeout_seconds=settings.weather_request_timeout_seconds,
        cache_ttl_minutes=settings.weather_cache_ttl_minutes,
        max_retries=settings.weather_max_retries,
        retry_backoff_seconds=settings.weather_retry_backoff_seconds,
        rate_limit_per_minute=settings.weather_rate_limit_per_minute,
    )
    return provider.fetch_raw(request)


def _format_as_of(value, issued_at: datetime) -> str:
    if isinstance(value, str) and value:
        return value.replace("-", "").replace(":", "")[:16]
    return issued_at.strftime("%Y%m%dT%H%MZ")


def _source_version(raw: dict, issued_at: datetime) -> str:
    model = str(raw.get("model") or raw.get("model_id") or raw.get("source_model") or "forecast_api")
    generation = raw.get("generationtime_ms")
    as_of = _format_as_of(raw.get("as_of") or raw.get("issued_at") or raw.get("generation_time"), issued_at)
    parts = [model[:24], f"asof={as_of}"]
    if generation is not None:
        parts.append(f"gen_ms={generation}")
    return ";".join(parts)[:64]


def _infer_confidence(raw: dict) -> str:
    declared = raw.get("quality") or raw.get("confidence")

    hourly = raw.get("hourly", {})
    times = hourly.get("time") or []
    if not times:
        return "low"
    expected_len = len(times)
    for key in REQUIRED_HOURLY_SERIES:
        values = hourly.get(key)
        if not isinstance(values, list) or len(values) != expected_len:
            return "low"
        if key != "time" and all(_safe_number(v) is None for v in values):
            return "low"
    for key in OPTIONAL_HOURLY_SERIES:
        values = hourly.get(key)
        if values is not None and len(values) != expected_len:
            return "low"
    return declared if declared in ALLOWED_CONFIDENCE else "nominal"


def _safe_number(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_provider_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def store_weather_forecast(db: Session, station: Station, raw: dict) -> list[WeatherForecast]:
    issued_at = utcnow()
    confidence = _infer_confidence(raw)
    source_version = _source_version(raw, issued_at)
    is_synthetic = bool(raw.get("is_synthetic"))
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])
    ghi = hourly.get("shortwave_radiation", [])
    dni = hourly.get("direct_normal_irradiance", [])
    dhi = hourly.get("diffuse_radiation", [])
    cloud = hourly.get("cloud_cover", [])
    temp = hourly.get("temperature_2m", [])
    precipitation = hourly.get("precipitation", [])
    wind = hourly.get("wind_speed_10m", [])

    created = []
    for i, t in enumerate(times):
        interval_start = _parse_provider_timestamp(t)
        wf = WeatherForecast(
            station_id=station.id,
            issued_at=issued_at,
            interval_start=interval_start,
            interval_end=interval_start + timedelta(hours=1),
            source=str(raw.get("provider") or settings.weather_provider),
            source_version=source_version,
            ghi_w_m2=_safe_get(ghi, i),
            dni_w_m2=_safe_get(dni, i),
            dhi_w_m2=_safe_get(dhi, i),
            cloud_cover_percent=_safe_get(cloud, i),
            temperature_c=_safe_get(temp, i),
            precipitation_mm=_safe_get(precipitation, i),
            wind_speed_ms=_safe_get(wind, i),
            confidence=confidence,
            is_synthetic=is_synthetic,
        )
        db.add(wf)
        created.append(wf)
    db.flush()
    return created


def _safe_get(arr: list, i: int):
    if i < len(arr) and arr[i] is not None:
        return _safe_number(arr[i])
    return None


def refresh_weather_for_station(db: Session, station: Station) -> list[WeatherForecast]:
    if station.latitude is None or station.longitude is None:
        raise WeatherUnavailableError("Statia nu are coordonate (latitudine/longitudine) configurate.")
    raw = fetch_forecast_raw(float(station.latitude), float(station.longitude))
    return store_weather_forecast(db, station, raw)
