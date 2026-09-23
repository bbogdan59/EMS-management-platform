from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from freezegun import freeze_time

from app.core.rate_limit import reset_key
from app.models.forecast import PvForecast
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _add_pv_forecast(db, station, interval_start: datetime, predicted_power_kw: Decimal):
    db.add(
        PvForecast(
            station_id=station.id,
            issued_at=interval_start - timedelta(hours=6),
            interval_start=interval_start,
            interval_end=interval_start + timedelta(minutes=15),
            scenario="expected",
            predicted_power_kw=predicted_power_kw,
        )
    )
    db.flush()


def test_horizon_hours_zero_keeps_the_legacy_past_only_window(client, db):
    """Comportamentul implicit (horizon_hours=0) trebuie sa ramana neschimbat
    -- doar trecutul, ca inainte de extinderea orizontului (cerere client:
    prognoza PV pe o durata mai lunga in fata)."""
    reset_key("login_attempts:testclient")
    user = make_user(db, email="forecast-horizon-zero@test.local", password="Password1234")
    org = make_org(db, "Forecast Horizon Zero Org")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="Forecast Horizon Zero Station")
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    _add_pv_forecast(db, station, now - timedelta(hours=1), Decimal("2.5"))
    _add_pv_forecast(db, station, now + timedelta(hours=2), Decimal("3.1"))
    db.commit()

    login(client, user.email, "Password1234")
    with freeze_time(now):
        resp = client.get(f"/stations/{station.id}/data/forecast-vs-actual?metric=pv&range=24h")

    assert resp.status_code == 200
    body = resp.json()
    starts = [row["t"] for row in body]
    assert (now - timedelta(hours=1)).isoformat() in starts
    assert (now + timedelta(hours=2)).isoformat() not in starts


def test_horizon_hours_extends_the_window_into_the_future(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="forecast-horizon@test.local", password="Password1234")
    org = make_org(db, "Forecast Horizon Org")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="Forecast Horizon Station")
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    _add_pv_forecast(db, station, now - timedelta(hours=1), Decimal("2.5"))
    future_start = now + timedelta(hours=20)
    _add_pv_forecast(db, station, future_start, Decimal("3.1"))
    db.commit()

    login(client, user.email, "Password1234")
    with freeze_time(now):
        resp = client.get(
            f"/stations/{station.id}/data/forecast-vs-actual?metric=pv&range=24h&horizon_hours=36"
        )

    assert resp.status_code == 200
    body = resp.json()
    starts = [row["t"] for row in body]
    assert future_start.isoformat() in starts
    future_row = next(row for row in body if row["t"] == future_start.isoformat())
    assert future_row["forecast_kw"] == 3.1
    assert future_row["actual_kw"] is None  # inca nu exista telemetrie pentru viitor


def test_horizon_hours_is_capped_at_72(client, db):
    """72h = fereastra meteo Open-Meteo (forecast_days=3) -- peste asta nu
    exista deja prognoza generata, indiferent de cerere."""
    reset_key("login_attempts:testclient")
    user = make_user(db, email="forecast-horizon-cap@test.local", password="Password1234")
    org = make_org(db, "Forecast Horizon Cap Org")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="Forecast Horizon Cap Station")
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get(f"/stations/{station.id}/data/forecast-vs-actual?metric=pv&range=24h&horizon_hours=73")

    assert resp.status_code == 422
