from __future__ import annotations

import contextlib

from app.services.consumption_forecast_service import ConsumptionForecastError
from app.workers import tasks


class _ScalarResult:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def all(self) -> list:
        return self.rows


class _Db:
    def __init__(self, stations: list) -> None:
        self.stations = stations

    def scalars(self, _query) -> _ScalarResult:
        return _ScalarResult(self.stations)


class _Station:
    def __init__(self, station_id: str) -> None:
        self.id = station_id


@contextlib.contextmanager
def _lock(acquired: bool):
    yield acquired


@contextlib.contextmanager
def _session(db):
    yield db


def test_weather_and_forecast_task_reports_stage_counts(monkeypatch):
    stations = [_Station("s1"), _Station("s2")]
    monkeypatch.setattr(tasks, "_task_lock", lambda name: _lock(True))
    monkeypatch.setattr(tasks, "session_scope", lambda: _session(_Db(stations)))
    monkeypatch.setattr(tasks.weather_service, "refresh_weather_for_station", lambda db, station: None)
    monkeypatch.setattr(tasks.pv_forecast_service, "generate_pv_forecast", lambda db, station: None)
    monkeypatch.setattr(tasks, "generate_consumption_forecast", lambda db, station, start, end: None)

    result = tasks.weather_and_forecast_task()

    assert result["stations_processed"] == 2
    assert result["stages"] == {
        "weather": {"succeeded": 2, "failed": 0},
        "pv": {"succeeded": 2, "failed": 0},
        "consumption": {"succeeded": 2, "failed": 0},
    }
    assert result["errors"] == []


def test_weather_and_forecast_task_keeps_stage_failures_separate(monkeypatch):
    stations = [_Station("s1")]
    monkeypatch.setattr(tasks, "_task_lock", lambda name: _lock(True))
    monkeypatch.setattr(tasks, "session_scope", lambda: _session(_Db(stations)))

    def _weather_fails(db, station):
        raise RuntimeError("provider unavailable")

    def _consumption_fails(db, station, start, end):
        raise ConsumptionForecastError("insufficient coverage")

    monkeypatch.setattr(tasks.weather_service, "refresh_weather_for_station", _weather_fails)
    monkeypatch.setattr(tasks.pv_forecast_service, "generate_pv_forecast", lambda db, station: None)
    monkeypatch.setattr(tasks, "generate_consumption_forecast", _consumption_fails)

    result = tasks.weather_and_forecast_task()

    assert result["stations_processed"] == 1
    assert result["stages"]["weather"] == {"succeeded": 0, "failed": 1}
    assert result["stages"]["pv"] == {"succeeded": 1, "failed": 0}
    assert result["stages"]["consumption"] == {"succeeded": 0, "failed": 1}
    assert result["errors"] == [
        "s1: weather: provider unavailable",
        "s1: consumption: insufficient coverage",
    ]


def test_weather_and_forecast_task_reports_skipped_lock(monkeypatch):
    monkeypatch.setattr(tasks, "_task_lock", lambda name: _lock(False))

    assert tasks.weather_and_forecast_task() == {"skipped": "already_running"}


def test_opcom_import_daily_task_uses_publication_polling_policy(monkeypatch):
    db = object()
    monkeypatch.setattr(tasks, "_task_lock", lambda name: _lock(True))
    monkeypatch.setattr(tasks, "session_scope", lambda: _session(db))
    calls = []

    def poll(session):
        calls.append(session)
        return {"skipped": "already_received"}

    monkeypatch.setattr(tasks.opcom_service, "poll_next_day_prices", poll)
    assert tasks.opcom_import_daily_task() == {"skipped": "already_received"}
    assert calls == [db]


def test_opcom_import_daily_task_skips_when_already_running(monkeypatch):
    monkeypatch.setattr(tasks, "_task_lock", lambda name: _lock(False))
    assert tasks.opcom_import_daily_task() == {"skipped": "already_running"}
