from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.forecast import PvForecast, WeatherForecast
from app.models.notification import Notification, NotificationDelivery
from app.models.organization import Organization
from app.models.user import User
from app.services import morning_briefing_service as service
from app.services import notification_service as notifications
from tests.factories import make_membership, make_org, make_station, make_user
from tests.integration.test_station_notifications import aggregate
from tests.web_helpers import login


def setup(db, suffix=""):
    user = make_user(db, email=f"morning{suffix}@test.local")
    org = make_org(db)
    member = make_membership(db, user, org, "organization_admin")
    station = make_station(db, org, user)
    pref = notifications.preference(db, user, org)
    pref.morning_briefing = True
    pref.quiet_start = pref.quiet_end = 0
    pref.verified_email = user.email
    pref.matrix = {"briefing:info:email": "immediate"}
    db.flush()
    return user, org, member, station, pref


def forecasts(db, station, now, power="1"):
    day = now.astimezone(ZoneInfo(station.timezone)).date()
    start, end = service.day_bounds(station, day)
    rows = []
    while start < end:
        weather = WeatherForecast(
            station_id=station.id,
            issued_at=now,
            interval_start=start,
            interval_end=start + timedelta(hours=1),
            confidence="high",
        )
        db.add(weather)
        db.flush()
        row = PvForecast(
            station_id=station.id,
            issued_at=now,
            interval_start=start,
            interval_end=weather.interval_end,
            predicted_power_kw=Decimal(power),
            based_on_weather_forecast_id=weather.id,
            confidence="high",
            source_version="test-v1",
        )
        db.add(row)
        rows.append(row)
        start = weather.interval_end
    db.flush()
    return rows


@pytest.fixture()
def context(db):
    return (*setup(db), datetime(2026, 9, 29, 6, tzinfo=UTC))


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2024, 2, 29), 24)]
)
def test_briefing_integrates_actual_station_day_and_keeps_immutable_facts(db, day, hours):
    user, org, member, station, pref = setup(db)
    now = datetime.combine(day, datetime.min.time(), ZoneInfo(station.timezone)).astimezone(
        UTC
    ) + timedelta(hours=8)
    rows = forecasts(db, station, now)
    assert service.materialize(db, now) == 1
    notice = db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert Decimal(notice.payload["expected_kwh"]) == hours
    assert notice.payload["display_kwh"] in notice.payload["body"]
    assert notice.payload["source_versions"] == ["pvlib:test-v1"]
    assert "pret" not in notice.payload["body"] and "RON" not in notice.payload["body"]
    saved = dict(notice.payload)
    rows[0].predicted_power_kw = 9
    notice.read_at = now
    db.flush()
    assert service.materialize(db, now + timedelta(minutes=1)) == 0
    db.refresh(notice)
    assert notice.payload == saved and notice.read_at == now


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "gap",
        "low",
        "simulated",
        "weather_missing",
        "weather_simulated",
        "weather_low",
        "weather_stale",
    ],
)
def test_untrustworthy_forecasts_never_create_briefings(db, context, failure):
    user, org, member, station, pref, now = context
    rows = forecasts(db, station, now)
    row = rows[12]
    weather = db.get(WeatherForecast, row.based_on_weather_forecast_id)
    if failure == "stale":
        for item in rows:
            item.issued_at = now - timedelta(hours=7)
    elif failure == "gap":
        db.delete(row)
    elif failure == "low":
        row.confidence = "low"
    elif failure == "simulated":
        row.is_synthetic = True
    elif failure == "weather_missing":
        row.based_on_weather_forecast_id = None
    elif failure == "weather_simulated":
        weather.is_synthetic = True
    elif failure == "weather_low":
        weather.confidence = "low"
    else:
        weather.issued_at = now - timedelta(hours=7)
    db.flush()
    assert service.materialize(db, now) == 0
    assert (
        db.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id)) == 0
    )


def test_night_gap_is_allowed_but_daytime_gap_is_not(db, context):
    user, org, member, station, pref, now = context
    rows = forecasts(db, station, now)
    for row in rows[:3]:
        db.delete(row)
    db.flush()
    assert service.forecast_facts(db, station, now) is not None
    db.delete(rows[12])
    db.flush()
    assert service.forecast_facts(db, station, now) is None


def test_baseline_excludes_other_seasons_partial_and_synthetic_days(db, context):
    user, org, member, station, pref, now = context
    forecasts(db, station, now)
    day = now.date()
    for ago in range(1, 8):
        aggregate(db, station, day - timedelta(days=ago), pv_energy_kwh=Decimal(10))
    aggregate(
        db, station, day - timedelta(days=8), pv_energy_kwh=Decimal(100), coverage={"pv": 0.5}
    )
    aggregate(
        db, station, day - timedelta(days=9), pv_energy_kwh=Decimal(100), data_quality="simulated"
    )
    aggregate(db, station, day - timedelta(days=180), pv_energy_kwh=Decimal(100))
    facts = service.forecast_facts(db, station, now)
    assert facts["classification"] == "high" and facts["baseline"]["days"] == 7
    assert Decimal(facts["baseline"]["value_kwh"]) == 10
    assert "10.0 kWh" in facts["body"]


def test_window_clock_quiet_hours_and_dst_repeated_hour(db, context):
    user, org, member, station, pref, now = context
    pref.timezone = "America/New_York"
    assert service.in_window(pref, station, now)
    pref.briefing_clock = "user"
    assert not service.in_window(pref, station, now)
    pref.briefing_clock = "station"
    pref.timezone = station.timezone
    pref.briefing_start_hour, pref.briefing_end_hour = 3, 4
    repeated = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert service.in_window(pref, station, repeated)
    assert service.in_window(pref, station, repeated + timedelta(hours=1))
    assert service.window_expiry(pref, station, repeated) == repeated.replace(hour=2, minute=0)
    pref.quiet_start, pref.quiet_end = 22, 8
    assert not service.in_window(pref, station, repeated)


def test_expiry_covers_source_weather_age_and_coalescing_fits_window(db, context):
    user, org, member, station, pref, now = context
    rows = forecasts(db, station, now)
    weather = db.get(WeatherForecast, rows[12].based_on_weather_forecast_id)
    weather.issued_at = now - timedelta(hours=5, minutes=58)
    db.flush()
    facts = service.forecast_facts(db, station, now)
    assert datetime.fromisoformat(facts["expires_at"]) == now + timedelta(minutes=2)
    service.materialize(db, now)
    delivery = db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.user_id == user.id)
    )
    assert now <= delivery.due_at < delivery.expires_at


def test_multiple_stations_coalesce_delivery_and_retry_rechecks_revocation(
    db, context, monkeypatch
):
    user, org, member, station, pref, now = context
    monkeypatch.setattr(get_settings(), "notifications_email_enabled", True)
    forecasts(db, station, now)
    second = make_station(db, org, user, name="Second roof")
    forecasts(db, second, now)
    assert service.materialize(db, now) == 2
    delivery = db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.user_id == user.id)
    )
    assert len(delivery.notification_ids) == 2
    assert (
        db.scalar(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.user_id == user.id
            )
        )
        == 1
    )
    adapter = Mock()
    adapter.send.side_effect = RuntimeError("sensitive provider error")
    assert notifications.deliver_one(db, now + timedelta(minutes=11), email_adapter=adapter)
    assert delivery.attempts == 1 and delivery.failure_code == "adapter_failure"
    assert adapter.send.call_args.args[1] == "Briefing matinal: 2 statii"
    member.is_active = False
    db.flush()
    assert notifications.deliver_one(db, now + timedelta(minutes=14), email_adapter=adapter)
    assert delivery.status == "suppressed" and adapter.send.call_count == 1


@pytest.mark.parametrize("revoke", ["optout", "category", "expired", "forecast_age", "station"])
def test_pending_delivery_rechecks_optout_and_expiry(db, context, monkeypatch, revoke):
    user, org, member, station, pref, now = context
    monkeypatch.setattr(get_settings(), "notifications_email_enabled", True)
    forecasts(db, station, now)
    service.materialize(db, now)
    delivery = db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.user_id == user.id)
    )
    due = now + timedelta(minutes=11)
    if revoke == "optout":
        pref.morning_briefing = False
    elif revoke == "category":
        pref.matrix = {}
    elif revoke == "expired":
        delivery.expires_at = now
    elif revoke == "station":
        station.is_active = False
    else:
        notice = db.scalar(select(Notification).where(Notification.user_id == user.id))
        notice.payload = {
            **notice.payload,
            "forecast_issued_at": (now - timedelta(hours=6)).isoformat(),
        }
    db.flush()
    adapter = Mock()
    notifications.deliver_one(db, due, email_adapter=adapter)
    assert delivery.status == "suppressed" and not adapter.send.called


def test_briefing_deep_link_requires_current_membership_and_own_notice(db, client, context):
    user, org, member, station, pref, now = context
    forecasts(db, station, now)
    service.materialize(db, now)
    db.commit()
    login(client, user.email, "TestPass1234")
    url = f"/stations/{station.id}/briefing/{now.date()}"
    response = client.get(url)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "24.0" in response.text
    feed = f"/stations/{station.id}/notifications"
    assert len(client.get(feed + "?kind=briefing").json()["items"]) == 1
    assert client.get(feed + "?kind=summary").json()["items"] == []
    member.is_active = False
    db.commit()
    assert client.get(url).status_code == 403
    other = make_user(db, email="other-briefing@test.local")
    make_membership(db, other, org, "viewer")
    db.commit()
    login(client, other.email, "TestPass1234")
    assert client.get(url).status_code == 404


def test_concurrent_workers_create_one_notice_and_delivery(engine):
    now = datetime(2026, 9, 29, 6, tzinfo=UTC)
    with Session(engine) as db:
        user, org, member, station, pref = setup(db, uuid4().hex[:8])
        forecasts(db, station, now)
        user_id, org_id = user.id, org.id
        db.commit()
    locked, release = Event(), Event()

    def run(hold=False):
        with Session(engine) as db:
            service.materialize(db, now)
            if hold:
                locked.set()
                assert release.wait(10)
            db.commit()

    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(run, True)
            assert locked.wait(10)
            second = pool.submit(run)
            second.result(10)
            release.set()
            first.result(10)
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count(Notification.id)).where(Notification.user_id == user_id)
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count(NotificationDelivery.id)).where(
                        NotificationDelivery.user_id == user_id
                    )
                )
                == 1
            )
    finally:
        release.set()
        with Session(engine) as db:
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
