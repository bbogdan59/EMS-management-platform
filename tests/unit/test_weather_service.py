from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.services import weather_service
from app.services.weather_service import (
    OpenMeteoWeatherProvider,
    WeatherProviderRequest,
    WeatherUnavailableError,
)


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.ttls[key] = ttl
        self.values[key] = value

    def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


class _Response:
    def __init__(self, payload: dict, error: httpx.HTTPError | None = None) -> None:
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> dict:
        return self.payload


class _FakeClient:
    def __init__(self, calls: list[dict], payload: dict, error: httpx.HTTPError | None = None) -> None:
        self.calls = calls
        self.payload = payload
        self.error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str, params: dict) -> _Response:
        self.calls.append({"url": url, "params": params})
        return _Response(self.payload, self.error)


class _SequencedClient:
    def __init__(self, calls: list[dict], responses: list[_Response]) -> None:
        self.calls = calls
        self.responses = responses

    def __enter__(self) -> _SequencedClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str, params: dict) -> _Response:
        self.calls.append({"url": url, "params": params})
        return self.responses.pop(0)


class _FakeDb:
    def __init__(self) -> None:
        self.rows = []
        self.flushed = False

    def add(self, row) -> None:
        self.rows.append(row)

    def flush(self) -> None:
        self.flushed = True


class _Station:
    id = "station-id"


def test_open_meteo_provider_fetches_utc_payload_and_caches(monkeypatch):
    redis = _MemoryRedis()
    calls: list[dict] = []
    payload = {"hourly": {"time": ["2026-01-01T00:00"]}, "generationtime_ms": 12.3}

    monkeypatch.setattr(weather_service, "get_redis", lambda: redis)
    monkeypatch.setattr(weather_service.httpx, "Client", lambda timeout: _FakeClient(calls, payload))

    provider = OpenMeteoWeatherProvider()
    request = WeatherProviderRequest(
        latitude=44.4268,
        longitude=26.1025,
        base_url="https://weather.example.test/forecast",
        timeout_seconds=7,
        cache_ttl_minutes=15,
    )

    first = provider.fetch_raw(request)
    second = provider.fetch_raw(request)

    assert first == payload
    assert second == payload
    assert len(calls) == 1
    assert calls[0]["url"] == "https://weather.example.test/forecast"
    assert calls[0]["params"]["timezone"] == "UTC"
    assert calls[0]["params"]["forecast_days"] == 3
    assert "shortwave_radiation" in calls[0]["params"]["hourly"]
    assert "precipitation" in calls[0]["params"]["hourly"]
    cache_key = "weather_forecast:open-meteo:44.427:26.102"
    assert redis.ttls[cache_key] == 15 * 60


def test_fetch_forecast_raw_delegates_to_named_provider(monkeypatch):
    class _Provider:
        name = "test-provider"

        def __init__(self) -> None:
            self.requests: list[WeatherProviderRequest] = []

        def fetch_raw(self, request: WeatherProviderRequest) -> dict:
            self.requests.append(request)
            return {"ok": True}

    provider = _Provider()
    monkeypatch.setitem(weather_service.PROVIDERS, provider.name, provider)

    out = weather_service.fetch_forecast_raw(45.0, 25.0, provider_name=provider.name)

    assert out == {"ok": True}
    assert provider.requests[0].latitude == 45.0
    assert provider.requests[0].longitude == 25.0
    assert provider.requests[0].max_retries == weather_service.settings.weather_max_retries
    assert provider.requests[0].retry_backoff_seconds == weather_service.settings.weather_retry_backoff_seconds
    assert provider.requests[0].rate_limit_per_minute == weather_service.settings.weather_rate_limit_per_minute


def test_unknown_weather_provider_fails_without_silent_fallback():
    with pytest.raises(WeatherUnavailableError, match="Provider meteo neacceptat"):
        weather_service.get_weather_provider("missing-provider")


def test_open_meteo_provider_wraps_http_errors(monkeypatch):
    redis = _MemoryRedis()
    calls: list[dict] = []

    monkeypatch.setattr(weather_service, "get_redis", lambda: redis)
    monkeypatch.setattr(
        weather_service.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, {}, httpx.ConnectError("offline")),
    )

    provider = OpenMeteoWeatherProvider()
    request = WeatherProviderRequest(
        latitude=44.0,
        longitude=26.0,
        base_url="https://weather.example.test/forecast",
        timeout_seconds=7,
        cache_ttl_minutes=15,
    )

    with pytest.raises(WeatherUnavailableError, match="open-meteo"):
        provider.fetch_raw(request)

    assert calls


def test_open_meteo_provider_retries_before_failing_and_caches_success(monkeypatch):
    redis = _MemoryRedis()
    calls: list[dict] = []
    sleeps: list[float] = []
    payload = {"hourly": {"time": ["2026-01-01T00:00"]}, "generationtime_ms": 1.1}
    responses = [_Response({}, httpx.ConnectError("temporary offline")), _Response(payload)]

    monkeypatch.setattr(weather_service, "get_redis", lambda: redis)
    monkeypatch.setattr(weather_service.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(weather_service.httpx, "Client", lambda timeout: _SequencedClient(calls, responses))

    provider = OpenMeteoWeatherProvider()
    request = WeatherProviderRequest(
        latitude=44.0,
        longitude=26.0,
        base_url="https://weather.example.test/forecast",
        timeout_seconds=7,
        cache_ttl_minutes=15,
        max_retries=2,
        retry_backoff_seconds=0.1,
    )

    assert provider.fetch_raw(request) == payload
    assert provider.fetch_raw(request) == payload
    assert len(calls) == 2
    assert sleeps == [0.1]


def test_open_meteo_provider_rate_limits_uncached_provider_calls(monkeypatch):
    redis = _MemoryRedis()
    calls: list[dict] = []
    payload = {"hourly": {"time": ["2026-01-01T00:00"]}, "generationtime_ms": 1.1}

    monkeypatch.setattr(weather_service, "get_redis", lambda: redis)
    monkeypatch.setattr(weather_service.httpx, "Client", lambda timeout: _FakeClient(calls, payload))

    provider = OpenMeteoWeatherProvider()
    request = WeatherProviderRequest(
        latitude=44.0,
        longitude=26.0,
        base_url="https://weather.example.test/forecast",
        timeout_seconds=7,
        cache_ttl_minutes=15,
        rate_limit_per_minute=1,
    )

    assert provider.fetch_raw(request) == payload
    with pytest.raises(WeatherUnavailableError, match="Limita locala de apeluri meteo"):
        provider.fetch_raw(
            WeatherProviderRequest(
                latitude=45.0,
                longitude=25.0,
                base_url=request.base_url,
                timeout_seconds=request.timeout_seconds,
                cache_ttl_minutes=request.cache_ttl_minutes,
                rate_limit_per_minute=request.rate_limit_per_minute,
            )
        )

    assert len(calls) == 1
    rate_keys = [key for key in redis.ttls if key.startswith("weather_rate:open-meteo:")]
    assert len(rate_keys) == 1
    assert redis.ttls[rate_keys[0]] == 90


def test_store_weather_forecast_preserves_precipitation_and_unknowns():
    db = _FakeDb()
    raw = {
        "generationtime_ms": 3.5,
        "hourly": {
            "time": ["2026-01-01T00:00", "2026-01-01T01:00"],
            "shortwave_radiation": [0, 5],
            "direct_normal_irradiance": [None, 10],
            "diffuse_radiation": [0, 1],
            "cloud_cover": [90, 80],
            "temperature_2m": [2.5, 2.1],
            "precipitation": [0.4, None],
            "wind_speed_10m": [3.0, 3.5],
        },
        "as_of": "2026-01-01T00:00:00Z",
        "model": "open-meteo-gfs-romania",
        "provider": "open-meteo",
    }

    rows = weather_service.store_weather_forecast(db, _Station(), raw)

    assert db.flushed is True
    assert rows == db.rows
    assert rows[0].precipitation_mm == 0.4
    assert rows[1].precipitation_mm is None
    assert rows[0].source == "open-meteo"
    assert rows[0].source_version.startswith("open-meteo-gfs-romania;asof=20260101T000000Z"[:40])
    assert rows[0].confidence == "nominal"
    assert rows[0].is_synthetic is False


def test_store_weather_forecast_converts_provider_offsets_to_utc_instants():
    db = _FakeDb()
    raw = {
        "provider": "weather-test",
        "hourly": {
            "time": ["2026-06-01T03:00:00+03:00", "2026-06-01T01:00:00Z"],
            "shortwave_radiation": [0, 10],
            "cloud_cover": [80, 70],
            "temperature_2m": [20, 19],
        },
    }

    rows = weather_service.store_weather_forecast(db, _Station(), raw)

    assert rows[0].interval_start == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    assert rows[1].interval_start == datetime(2026, 6, 1, 1, 0, tzinfo=UTC)


def test_store_weather_forecast_marks_incomplete_payload_low_confidence_without_zero_fill():
    db = _FakeDb()
    raw = {
        "provider": "weather-test",
        "quality": "high",
        "is_synthetic": True,
        "hourly": {
            "time": ["2026-01-01T00:00", "2026-01-01T01:00"],
            "shortwave_radiation": [None, None],
            "cloud_cover": [80, 70],
            # temperature_2m missing: payload is incomplete and must degrade
        },
    }

    rows = weather_service.store_weather_forecast(db, _Station(), raw)

    assert rows[0].ghi_w_m2 is None
    assert rows[1].temperature_c is None
    assert rows[0].confidence == "low"
    assert rows[0].is_synthetic is True


def test_weather_confidence_degrades_when_required_series_are_missing():
    db = _FakeDb()
    raw = {
        "hourly": {
            "time": ["2026-01-01T00:00"],
            "shortwave_radiation": [100],
            "cloud_cover": [20],
            # temperature_2m missing -> low confidence, but no invented value
        },
    }

    rows = weather_service.store_weather_forecast(db, _Station(), raw)

    assert rows[0].temperature_c is None
    assert rows[0].confidence == "low"


def test_weather_non_finite_required_values_are_missing_and_low_confidence():
    db = _FakeDb()
    raw = {
        "provider": "weather-test",
        "hourly": {
            "time": ["2026-01-01T00:00", "2026-01-01T01:00"],
            "shortwave_radiation": [float("nan"), "Infinity"],
            "cloud_cover": [20, 25],
            "temperature_2m": [3, 4],
        },
    }

    rows = weather_service.store_weather_forecast(db, _Station(), raw)

    assert rows[0].ghi_w_m2 is None
    assert rows[1].ghi_w_m2 is None
    assert rows[0].confidence == "low"
