from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.rate_limit import reset_key
from app.models.enums import ImportRunStatus
from app.models.market import ImportRun, MarketPriceInterval
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _add_market_interval(db, interval_start: datetime, price_lei_per_kwh: Decimal):
    run = ImportRun(
        source="opcom_pzu", delivery_date=interval_start.date(), revision=1,
        status=ImportRunStatus.succeeded.value, source_url="https://test.local",
        interval_count=1,
    )
    db.add(run)
    db.flush()
    db.add(
        MarketPriceInterval(
            import_run_id=run.id, source="opcom_pzu", delivery_date=interval_start.date(), revision=1,
            interval_index=1, interval_start=interval_start, interval_end=interval_start + timedelta(minutes=15),
            currency="RON", price_lei_per_mwh=price_lei_per_kwh * Decimal("1000"),
            price_lei_per_kwh=price_lei_per_kwh, is_negative=price_lei_per_kwh < 0, is_current=True,
        )
    )
    db.flush()


def test_dashboard_prices_today_uses_station_timezone(client, db):
    from freezegun import freeze_time

    reset_key("login_attempts:testclient")
    user = make_user(db, email="prices-timezone@test.local", password="Password1234")
    org = make_org(db, "Prices Timezone Org")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="Prices Timezone Station", timezone="America/New_York")
    _add_market_interval(db, datetime(2026, 9, 9, 12, 0, tzinfo=UTC), Decimal("0.25"))
    db.commit()

    login(client, user.email, "Password1234")
    with freeze_time("2026-09-10 01:00:00+00:00"):
        resp = client.get(f"/stations/{station.id}/data/prices?day=today")

    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2026-09-09"
    assert body["published"] is True
    assert body["intervals"][0]["price_lei_kwh"] == 0.25
