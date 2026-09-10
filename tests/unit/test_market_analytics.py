from __future__ import annotations

from datetime import date, timedelta

from app.core.security import utcnow
from app.services import market_analytics_service as market
from tests.factories import make_market_day


def test_get_daily_averages_aggregates_correctly(db):
    make_market_day(db, date(2025, 6, 1), [100.0, 200.0, 300.0, 400.0])
    daily = market.get_daily_averages(db, years=[2025])
    assert len(daily) == 1
    row = daily[0]
    assert row["avg_price_lei_mwh"] == 250.0
    assert row["min_price_lei_mwh"] == 100.0
    assert row["max_price_lei_mwh"] == 400.0
    assert row["sample_count"] == 4
    assert row["avg_price_lei_kwh"] == 0.25


def test_get_daily_averages_excludes_superseded_revisions(db):
    make_market_day(db, date(2025, 6, 1), [100.0], revision=1)
    from sqlalchemy import select
    from app.models.market import MarketPriceInterval

    # marcheaza revizia 1 ca nemaifiind curenta si adauga o revizie 2
    for row in db.scalars(select(MarketPriceInterval).where(MarketPriceInterval.delivery_date == date(2025, 6, 1))):
        row.is_current = False
        db.add(row)
    make_market_day(db, date(2025, 6, 1), [500.0], revision=2)

    daily = market.get_daily_averages(db, years=[2025])
    assert len(daily) == 1
    assert daily[0]["avg_price_lei_mwh"] == 500.0  # doar revizia curenta e folosita


def test_get_year_over_year_overlay_groups_by_year(db):
    make_market_day(db, date(2024, 3, 15), [200.0])
    make_market_day(db, date(2025, 3, 15), [250.0])
    overlay = market.get_year_over_year_overlay(db, years=[2024, 2025])
    assert set(overlay.keys()) == {2024, 2025}
    assert overlay[2024][0]["month_day"] == "03-15"
    assert overlay[2025][0]["avg_price_lei_mwh"] == 250.0


def test_get_monthly_averages(db):
    make_market_day(db, date(2025, 1, 5), [100.0])
    make_market_day(db, date(2025, 1, 15), [300.0])
    make_market_day(db, date(2025, 2, 1), [500.0])
    monthly = market.get_monthly_averages(db, years=[2025])
    months = {r["month"]: r["avg_price_lei_mwh"] for r in monthly[2025]}
    assert months[1] == 200.0  # (100+300)/2
    assert months[2] == 500.0


def test_forecast_seasonal_baseline_with_trend_adjustment(db):
    today = utcnow().date()
    prior_year_1 = today.year - 1
    prior_year_2 = today.year - 2
    baseline_price = 300.0
    actual_recent_price = 330.0  # +10% fata de baza sezoniera

    future_check_date = today + timedelta(days=10)
    relevant_days = [today - timedelta(days=offset) for offset in range(30)] + [future_check_date]

    for d in relevant_days:
        for prior_year in (prior_year_1, prior_year_2):
            try:
                prior_date = d.replace(year=prior_year)
            except ValueError:
                continue  # 29 februarie fara corespondent
            make_market_day(db, prior_date, [baseline_price])

    for offset in range(30):
        make_market_day(db, today - timedelta(days=offset), [actual_recent_price])

    forecast = market.get_forecast_to_year_end(db, target_year=today.year)

    assert forecast["method"] == "seasonal_naive_trend_adjusted"
    assert abs(forecast["trend_ratio"] - 1.1) < 0.01

    predicted_by_date = {p["date"]: p["predicted_price_lei_mwh"] for p in forecast["points"]}
    assert future_check_date.isoformat() in predicted_by_date
    assert abs(predicted_by_date[future_check_date.isoformat()] - actual_recent_price) < 1.0


def test_forecast_falls_back_to_flat_average_without_prior_years(db):
    today = utcnow().date()
    for offset in range(5):
        make_market_day(db, today - timedelta(days=offset), [280.0])

    forecast = market.get_forecast_to_year_end(db, target_year=today.year)
    assert forecast["method"] == "flat_recent_average"
    assert forecast["trend_ratio"] is None
    if forecast["points"]:
        assert forecast["points"][0]["predicted_price_lei_mwh"] == 280.0


def test_timeline_split_marks_future_intervals(db):
    now = utcnow()
    yesterday = (now - timedelta(days=1)).date()
    tomorrow = (now + timedelta(days=1)).date()
    make_market_day(db, yesterday, [100.0, 100.0, 100.0, 100.0])
    make_market_day(db, tomorrow, [200.0, 200.0, 200.0, 200.0])

    series = market.get_timeline_split(db, now - timedelta(days=2), now + timedelta(days=2))
    assert any(not p["is_future"] for p in series)
    assert any(p["is_future"] for p in series)
    for p in series:
        if p["is_future"]:
            assert p["price_lei_mwh"] == 200.0
        else:
            assert p["price_lei_mwh"] == 100.0


def test_market_status_reports_today_tomorrow_and_missing(db):
    today = utcnow().date()
    make_market_day(db, today, [123.0])
    status = market.get_market_status(db)
    assert status["today"]["status"] == "succeeded"
    assert status["today"]["is_synthetic"] is False
    assert status["tomorrow"] is None  # nu a fost importata inca -- lipsa, nu fabricata
    assert status["total_days_available"] == 1
