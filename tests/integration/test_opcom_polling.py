from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from freezegun import freeze_time
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.market import ImportRun, MarketPriceInterval
from app.services import opcom_service
from app.services.opcom_fixtures import generate_synthetic_csv
from tests.factories import make_market_day


@pytest.fixture()
def opcom_source(monkeypatch):
    state = {"status": 200, "calls": [], "malformed": False}
    client_class = httpx.Client

    def respond(request):
        state["calls"].append(str(request.url))
        parts = request.url.path.split("/")
        delivery_date = date(int(parts[-2]), int(parts[-3]), int(parts[-4]))
        content = (
            "not yet published" if state["malformed"] else generate_synthetic_csv(delivery_date)
        )
        return httpx.Response(state["status"], text=content)

    monkeypatch.setattr(opcom_service.settings, "opcom_use_synthetic_fixture_on_failure", False)
    monkeypatch.setattr(
        opcom_service.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    return state


@pytest.mark.parametrize(
    "local_date", ["2026-01-15", "2026-07-15", "2026-03-29", "2026-10-25", "2024-02-28"]
)
def test_polling_starts_at_1315_bucharest_across_dst_and_leap_year(db, opcom_source, local_date):
    day = date.fromisoformat(local_date)
    cutoff = datetime.combine(day, opcom_service.PUBLICATION_POLL_START, opcom_service.BUCHAREST)
    delivery_date = day + timedelta(days=1)
    with freeze_time(cutoff - timedelta(seconds=1)):
        assert opcom_service.poll_next_day_prices(db) == {"skipped": "before_publication_window"}
    assert opcom_source["calls"] == []
    assert db.scalar(select(ImportRun.id).where(ImportRun.delivery_date == delivery_date)) is None

    with freeze_time(cutoff):
        assert opcom_service.poll_next_day_prices(db) == {delivery_date.isoformat(): "succeeded"}
    assert len(opcom_source["calls"]) == 1
    assert f"/{delivery_date:%d/%m/%Y}/ro" in opcom_source["calls"][0]
    with freeze_time(cutoff + timedelta(minutes=30)):
        assert opcom_service.poll_next_day_prices(db)["skipped"] == "already_received"
    with freeze_time(cutoff.replace(hour=23, minute=45)):
        assert opcom_service.poll_next_day_prices(db)["skipped"] == "already_received"
    assert len(opcom_source["calls"]) == 1


@pytest.mark.parametrize("malformed", [False, True])
def test_failed_or_unpublished_response_retries_every_half_hour_until_success(
    db, opcom_source, malformed
):
    opcom_source.update(status=200 if malformed else 503, malformed=malformed)
    with freeze_time("2026-09-25T10:15:00Z"):
        assert opcom_service.poll_next_day_prices(db) == {"2026-09-26": "failed"}
    # The transport must not perform the usual rapid HTTP retries.
    assert len(opcom_source["calls"]) == 1
    for at in ["10:15:05", "10:30:00", "10:44:59"]:
        with freeze_time(f"2026-09-25T{at}Z"):
            assert opcom_service.poll_next_day_prices(db)["skipped"] == "retry_not_due"
    assert len(opcom_source["calls"]) == 1
    with freeze_time("2026-09-25T10:45:00Z"):
        assert opcom_service.poll_next_day_prices(db) == {"2026-09-26": "failed"}
    assert len(opcom_source["calls"]) == 2
    opcom_source.update(status=200, malformed=False)
    with freeze_time("2026-09-25T11:15:00Z"):
        assert opcom_service.poll_next_day_prices(db) == {"2026-09-26": "succeeded"}
    with freeze_time("2026-09-25T11:45:00Z"):
        assert opcom_service.poll_next_day_prices(db)["skipped"] == "already_received"
    assert len(opcom_source["calls"]) == 3
    assert [
        run.attempt_count
        for run in db.scalars(select(ImportRun).where(ImportRun.delivery_date == date(2026, 9, 26)))
    ] == [1, 1, 1]


def test_existing_real_prices_skip_http_but_synthetic_prices_do_not(db, opcom_source):
    make_market_day(db, date(2026, 9, 26), [100], is_synthetic=False)
    make_market_day(db, date(2026, 9, 27), [100], is_synthetic=True)
    with freeze_time("2026-09-25T10:15:00Z"):
        assert opcom_service.poll_next_day_prices(db)["skipped"] == "already_received"
    assert opcom_source["calls"] == []
    with freeze_time("2026-09-26T10:15:00Z"):
        assert opcom_service.poll_next_day_prices(db) == {"2026-09-27": "succeeded"}
    assert len(opcom_source["calls"]) == 1


def test_next_day_resets_polling_without_morning_or_today_fetches(db, opcom_source):
    with freeze_time("2026-09-25T10:15:00Z"):
        opcom_service.poll_next_day_prices(db)
    for at in ["2026-09-25T21:00:00Z", "2026-09-26T00:15:00Z", "2026-09-26T10:14:59Z"]:
        with freeze_time(at):
            assert opcom_service.poll_next_day_prices(db)["skipped"] == "before_publication_window"
    assert len(opcom_source["calls"]) == 1
    with freeze_time("2026-09-26T10:15:00Z"):
        assert opcom_service.poll_next_day_prices(db) == {"2026-09-27": "succeeded"}
    assert len(opcom_source["calls"]) == 2


@pytest.mark.parametrize("status", [200, 503])
def test_concurrent_and_restarted_pollers_do_not_duplicate_requests(engine, opcom_source, status):
    day = date(2032, 7, 16)
    opcom_source["status"] = status
    barrier = Barrier(2)

    def attempt():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            result = opcom_service.poll_next_day_prices(db)
            db.commit()
            return result

    def cleanup():
        with Session(engine) as db:
            ids = [
                str(key)
                for key in db.scalars(select(ImportRun.id).where(ImportRun.delivery_date == day))
            ]
            db.execute(delete(Alert).where(Alert.context["import_run_id"].as_string().in_(ids)))
            db.execute(delete(MarketPriceInterval).where(MarketPriceInterval.delivery_date == day))
            db.execute(delete(ImportRun).where(ImportRun.delivery_date == day))
            db.commit()

    cleanup()
    try:
        with freeze_time("2032-07-15T10:15:00Z"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt), pool.submit(attempt)]
                results = [future.result(timeout=15) for future in futures]
            assert len(opcom_source["calls"]) == 1
            skipped = "already_received" if status == 200 else "retry_not_due"
            assert sum(result.get("skipped") == skipped for result in results) == 1
            # A fresh session represents a restarted worker, without in-memory state.
            with Session(engine) as db:
                assert opcom_service.poll_next_day_prices(db)["skipped"] == skipped
        with freeze_time("2032-07-15T10:45:00Z"), Session(engine) as db:
            opcom_service.poll_next_day_prices(db)
            db.commit()
        assert len(opcom_source["calls"]) == (1 if status == 200 else 2)
    finally:
        cleanup()


def test_beat_wakes_at_quarter_past_and_quarter_to_without_changing_other_timezones(monkeypatch):
    entries = [
        entry
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"] == "app.workers.tasks.opcom_import_daily_task"
    ]
    assert len(entries) == 1
    schedule = entries[0]["schedule"]
    monkeypatch.setattr(schedule, "nowfun", utcnow)
    assert celery_app.conf.timezone == "UTC"
    for at, expected in [
        ("10:14:00", False),
        ("10:15:00", True),
        ("10:30:00", False),
        ("10:45:00", True),
    ]:
        with freeze_time(f"2026-09-25T{at}Z"):
            assert schedule.is_due(utcnow() - timedelta(minutes=1)).is_due is expected


def test_synthetic_fallback_is_not_treated_as_received_prices(db, monkeypatch, opcom_source):
    monkeypatch.setattr(opcom_service.settings, "opcom_use_synthetic_fixture_on_failure", True)
    opcom_source["status"] = 503
    with freeze_time("2026-09-25T10:15:00Z"):
        assert opcom_service.poll_next_day_prices(db) == {"2026-09-26": "succeeded"}
    assert opcom_service.has_successful_real_import(db, date(2026, 9, 26)) is False
    with freeze_time("2026-09-25T10:45:00Z"):
        opcom_service.poll_next_day_prices(db)
    assert len(opcom_source["calls"]) == 2


def test_invocation_waiting_past_midnight_does_not_fetch_old_target(db, monkeypatch, opcom_source):
    with freeze_time("2026-09-25T20:59:59Z") as clock:
        lock = opcom_service._lock_import_day

        def delayed_lock(session, source, day):
            lock(session, source, day)
            clock.tick(timedelta(seconds=2))

        monkeypatch.setattr(opcom_service, "_lock_import_day", delayed_lock)
        assert opcom_service.poll_next_day_prices(db)["skipped"] == "publication_window_expired"
    assert opcom_source["calls"] == []
