from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.security import utcnow
from app.services import market_analytics_service as market
from app.services.market_analytics_service import BUCHAREST
from tests.factories import make_market_day


@pytest.fixture(autouse=True)
def fixed_market_clock():
    from freezegun import freeze_time

    with freeze_time("2026-09-10 12:00:00"):
        yield


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
    # Referinta de "azi" trebuie sa fie aceeasi zi calendaristica pe care o
    # foloseste get_forecast_to_year_end (Europe/Bucharest, nu UTC) --
    # altfel testul devine nedeterminist in fereastra ~21:00-23:59 UTC, cand
    # cele doua zile difera (bug real, gasit in CI la exact aceasta ora).
    today = datetime.now(BUCHAREST).date()
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
    today = datetime.now(BUCHAREST).date()  # vezi comentariul din testul anterior
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
    today = datetime.now(BUCHAREST).date()  # get_market_status foloseste ziua locala, nu UTC
    make_market_day(db, today, [123.0])
    status = market.get_market_status(db)
    assert status["today"]["status"] == "succeeded"
    assert status["today"]["is_synthetic"] is False
    assert status["tomorrow"] is None  # nu a fost importata inca -- lipsa, nu fabricata
    assert status["total_days_available"] == 1


# --- Regresii pentru review-ul PR #7 -----------------------------------

def test_daily_averages_exclude_synthetic_fixture_by_default(db):
    """Un import sintetic nu trebuie sa contamineze mediile/exportul "reale",
    indiferent daca un import real ulterior a avut loc pentru alta zi."""
    make_market_day(db, date(2026, 9, 9), [900.0], is_synthetic=True)
    make_market_day(db, date(2026, 9, 8), [100.0], is_synthetic=False)

    daily = market.get_daily_averages(db)
    dates = {row["date"]: row for row in daily}
    assert date(2026, 9, 9) not in dates
    assert dates[date(2026, 9, 8)]["is_synthetic"] is False


def test_daily_averages_can_include_synthetic_with_provenance(db):
    make_market_day(db, date(2026, 9, 9), [900.0], is_synthetic=True)
    daily = market.get_daily_averages(db, include_synthetic=True)
    assert len(daily) == 1
    assert daily[0]["is_synthetic"] is True


def test_forecast_ignores_synthetic_history_even_with_later_real_import(db):
    """Reproducerea exacta din review: istoric sintetic la 900 lei/MWh pentru
    o zi recenta, plus un import real ulterior pentru alta zi -- forecast-ul
    nu trebuie sa "vada" deloc pretul sintetic, nici in baza sezoniera, nici
    in ajustarea de tendinta."""
    today = date(2026, 9, 10)
    make_market_day(db, today - timedelta(days=1), [900.0], is_synthetic=True)
    for offset in range(2, 32):
        make_market_day(db, today - timedelta(days=offset), [100.0], is_synthetic=False)

    forecast = market.get_forecast_to_year_end(db, target_year=today.year)
    assert forecast["method"] == "flat_recent_average"
    assert forecast["points"]
    assert forecast["points"][0]["predicted_price_lei_mwh"] == 100.0


def test_timeline_split_excludes_synthetic_intervals_by_default(db):
    now = utcnow()
    real_day = (now - timedelta(days=1)).date()
    synthetic_day = now.date()
    make_market_day(db, real_day, [100.0, 100.0, 100.0, 100.0])
    make_market_day(db, synthetic_day, [900.0, 900.0, 900.0, 900.0], is_synthetic=True)

    series = market.get_timeline_split(db, now - timedelta(days=2), now + timedelta(days=1))
    assert series
    assert all(p["price_lei_mwh"] == 100.0 for p in series)
    assert all(p["is_synthetic"] is False for p in series)


def test_seasonal_baseline_aligns_by_calendar_day_not_ordinal_day_of_year(db):
    """2024 e bisect (29 februarie exista), 2026 nu. Daca baza sezoniera ar
    fi cheiata dupa numarul ordinal "zi-din-an" (day_of_year) in loc de
    (luna, zi), predictia pentru 11 septembrie 2026 ar folosi din greseala
    pretul de la 10 septembrie 2024 (aceeasi zi-din-an dupa decalajul de 29
    februarie), nu pretul real de la 11 septembrie 2024."""
    # Preturi distincte pe zi (nu constante) ca sa nu se mascheze o eventuala
    # confuzie intre zile adiacente.
    make_market_day(db, date(2024, 9, 10), [910.0])
    make_market_day(db, date(2024, 9, 11), [911.0])
    make_market_day(db, date(2024, 9, 12), [912.0])
    # 30 de zile recente identice ca sa nu introduca o ajustare de tendinta
    # care sa ascunda decalajul testat.
    for offset in range(30):
        make_market_day(db, date(2026, 9, 10) - timedelta(days=offset), [911.0])

    # "Azi" e fixat explicit la 10 septembrie 2026 (prin ora locala
    # Bucuresti la ora amiezii, departe de orice granita de miezul noptii)
    # ca testul sa nu devina nedeterminist in functie de cand ruleaza cu
    # adevarat -- altfel "maine" (11 septembrie, exact ziua verificata mai
    # jos) ar putea sa nu mai fie in viitor daca ceasul real a trecut deja
    # de acea data.
    from freezegun import freeze_time

    with freeze_time("2026-09-10 10:00:00"):
        forecast = market.get_forecast_to_year_end(db, target_year=2026)
    predicted_by_date = {p["date"]: p["predicted_price_lei_mwh"] for p in forecast["points"]}
    assert predicted_by_date["2026-09-11"] == 911.0


def test_timeline_split_stays_raw_resolution_for_short_windows(db):
    """Fereastra implicita (30 zile, folosita de UI) trebuie sa ramana la
    rezolutia bruta a randurilor stocate, neschimbata -- pragul de agregare
    orara e strict PESTE 10 zile."""
    now = utcnow()
    day = (now - timedelta(days=1)).date()
    make_market_day(db, day, [100.0, 200.0, 300.0, 400.0])  # 4 randuri/zi (6h fiecare)

    series = market.get_timeline_split(db, now - timedelta(days=9), now + timedelta(days=1))  # fereastra = 10 zile exact

    assert len(series) == 4  # nicio agregare -- un punct per rand stocat


def test_timeline_split_aggregates_hourly_for_long_windows(db):
    """Peste pragul de agregare (>10 zile), numarul de puncte trebuie sa
    ramana rezonabil (pe ora, nu pe rezolutia bruta de import) indiferent
    cat de lunga e fereastra ceruta -- reproduce exact scenariul unui an
    intreg de istoric la 15 minute (issue #33/#35)."""
    now = utcnow()
    # O luna de zile la rezolutie de 15 minute (96 randuri/zi) -- ar insemna
    # ~2880 de puncte fara agregare, pentru o fereastra de doar 30 de zile.
    for offset in range(30):
        day = (now - timedelta(days=offset + 5)).date()
        make_market_day(db, day, [100.0] * 96)

    series = market.get_timeline_split(db, now - timedelta(days=40), now)

    assert 0 < len(series) <= 30 * 24  # cel mult un punct pe ora, nu unul pe rand brut
    assert all(abs(p["price_lei_mwh"] - 100.0) < 0.01 for p in series)


def test_timeline_split_hourly_aggregation_averages_within_bucket(db):
    now = utcnow()
    day = (now - timedelta(days=20)).date()
    # 4 randuri de 6h fiecare in aceeasi zi, cu preturi diferite -- cel putin
    # o "ora" din grup ramane distincta ca sa poata fi verificata media.
    make_market_day(db, day, [100.0, 300.0, 500.0, 700.0])

    series = market.get_timeline_split(db, now - timedelta(days=40), now)

    assert series  # agregarea ramane in interval, chiar daca fereastra e lunga
    prices = {p["price_lei_mwh"] for p in series}
    assert prices == {100.0, 300.0, 500.0, 700.0}  # fara suprapunere intre bucket-ele orare distincte


def test_timeline_split_hourly_aggregation_excludes_synthetic_by_default(db):
    now = utcnow()
    real_day = (now - timedelta(days=20)).date()
    synthetic_day = (now - timedelta(days=15)).date()
    make_market_day(db, real_day, [100.0] * 24)
    make_market_day(db, synthetic_day, [900.0] * 24, is_synthetic=True)

    series = market.get_timeline_split(db, now - timedelta(days=40), now)

    assert series
    assert all(p["price_lei_mwh"] != 900.0 for p in series)
    assert all(p["is_synthetic"] is False for p in series)


def test_timeline_split_stays_hourly_at_exactly_the_daily_threshold(db):
    """Fereastra de exact `TIMELINE_DAILY_THRESHOLD_DAYS` zile trebuie sa
    ramana la agregare orara -- pragul de agregare zilnica e strict PESTE
    acest numar de zile, la fel cum pragul orar e strict peste 10 zile."""
    now = utcnow()
    day = (now - timedelta(days=30)).date()
    make_market_day(db, day, [100.0, 300.0, 500.0, 700.0])  # 4 randuri de 6h

    series = market.get_timeline_split(
        db, now - timedelta(days=market.TIMELINE_DAILY_THRESHOLD_DAYS), now
    )

    assert {p["price_lei_mwh"] for p in series} == {100.0, 300.0, 500.0, 700.0}  # bucket-uri orare, nu zilnice


def test_timeline_split_aggregates_daily_for_year_long_windows(db):
    """Reproduce exact scenariul din issue #33: un an intreg de istoric la
    rezolutie de 15 minute (96 randuri/zi -- ~35.000 randuri fara agregare)
    trebuie sa produca un numar de puncte in ordinul sutelor (o zi = un
    punct), nu zeci de mii, nici macar mii (cat ar insemna agregarea orara)."""
    now = utcnow()
    for offset in range(120):
        day = (now - timedelta(days=offset + 5)).date()
        make_market_day(db, day, [100.0] * 96)

    series = market.get_timeline_split(db, now - timedelta(days=365), now)

    assert 0 < len(series) <= 366  # cel mult un punct pe zi
    assert all(abs(p["price_lei_mwh"] - 100.0) < 0.01 for p in series)


def test_timeline_split_daily_aggregation_averages_within_bucket(db):
    now = utcnow()
    day = (now - timedelta(days=100)).date()
    make_market_day(db, day, [100.0, 300.0])  # doua randuri in aceeasi zi

    series = market.get_timeline_split(db, now - timedelta(days=365), now)

    matching = [p for p in series if abs(p["price_lei_mwh"] - 200.0) < 0.01]
    assert matching  # media (100+300)/2 = 200, un singur punct pentru toata ziua


def test_timeline_split_daily_aggregation_excludes_synthetic_by_default(db):
    now = utcnow()
    real_day = (now - timedelta(days=100)).date()
    synthetic_day = (now - timedelta(days=90)).date()
    make_market_day(db, real_day, [100.0] * 4)
    make_market_day(db, synthetic_day, [900.0] * 4, is_synthetic=True)

    series = market.get_timeline_split(db, now - timedelta(days=365), now)

    assert series
    assert all(p["price_lei_mwh"] != 900.0 for p in series)
    assert all(p["is_synthetic"] is False for p in series)


def test_timeline_description_declares_contract_for_year_window():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2027, 1, 1, tzinfo=UTC)
    points = [{"t": "2026-01-01T00:00:00+00:00", "price_lei_mwh": 100.0} for _ in range(183)]

    payload = market.describe_timeline_split(start, end, points)

    assert payload["resolution"] == "1d"
    assert payload["aggregation"] == {"price_lei_mwh": "mean", "price_lei_kwh": "mean"}
    assert payload["timezone"] == "Europe/Bucharest"
    assert payload["coverage"] == 0.5014
    assert payload["points"] == points


def test_market_status_uses_bucharest_timezone_near_midnight(db):
    """22:00 UTC in septembrie e deja 01:00 a doua zi in Bucuresti (DST activ,
    UTC+3): "azi" trebuie sa fie ziua locala, nu cea UTC."""
    from freezegun import freeze_time

    make_market_day(db, date(2026, 9, 11), [123.0])
    with freeze_time("2026-09-10 22:00:00"):
        assert datetime.now(BUCHAREST).date() == date(2026, 9, 11)
        status = market.get_market_status(db)
    assert status["today"]["date"] == "2026-09-11"
    assert status["today"]["status"] == "succeeded"


def test_daily_timeline_aggregation_uses_bucharest_delivery_day_across_utc_midnight(db):
    """A Bucharest delivery day spans two UTC dates. Daily aggregation must
    use the explicit market delivery_date, not the DB session timezone."""
    from sqlalchemy import select

    from app.models.market import MarketPriceInterval

    delivery_day = date(2026, 6, 1)  # UTC+3: starts on 31 May at 21:00 UTC
    make_market_day(db, delivery_day, [100.0, 300.0])
    rows = db.scalars(
        select(MarketPriceInterval)
        .where(MarketPriceInterval.delivery_date == delivery_day)
        .order_by(MarketPriceInterval.interval_index)
    ).all()
    rows[0].interval_start = datetime.combine(delivery_day, time.min, tzinfo=UTC) - timedelta(hours=3)
    rows[0].interval_end = rows[0].interval_start + timedelta(hours=1)
    rows[1].interval_start = datetime.combine(delivery_day, time.min, tzinfo=UTC) + timedelta(hours=20)
    rows[1].interval_end = rows[1].interval_start + timedelta(hours=1)
    db.flush()

    series = market.get_timeline_split(
        db,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert len(series) == 1
    assert series[0]["price_lei_mwh"] == 200.0
    assert datetime.fromisoformat(series[0]["t"]).astimezone(BUCHAREST).date() == delivery_day
