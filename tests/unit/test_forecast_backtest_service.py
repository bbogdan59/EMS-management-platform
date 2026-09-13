from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.security import utcnow
from app.models.forecast import PvForecast
from app.models.telemetry import TelemetryAggregate
from app.services import forecast_backtest_service as backtest
from tests.factories import make_org, make_station, make_user


def _station(db, suffix=""):
    user = make_user(db, email=f"bt{suffix}@test.local")
    org = make_org(db, f"BT Org {suffix}")
    return make_station(db, org, user, name=f"BT Station {suffix}")


def _forecast(db, station, issued_at, interval_start, predicted_kw):
    db.add(
        PvForecast(
            station_id=station.id, issued_at=issued_at, interval_start=interval_start,
            interval_end=interval_start + timedelta(minutes=15), source="test",
            predicted_power_kw=Decimal(str(predicted_kw)), scenario="expected",
        )
    )


def _actual(db, station, period_start, pv_kwh, coverage=1.0):
    db.add(
        TelemetryAggregate(
            station_id=station.id, period_type="interval_15m", period_start=period_start,
            period_end=period_start + timedelta(minutes=15),
            pv_energy_kwh=Decimal(str(pv_kwh)) if pv_kwh is not None else None,
            coverage={"pv": coverage},
        )
    )


def test_backtest_computes_mae_and_bias_with_deterministic_fixture(db):
    station = _station(db, "mae")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    intervals = [t0 + timedelta(minutes=15 * i) for i in range(4)]
    # forecast_kw, actual_kwh (actual_kw = kwh*4)
    fixture = [
        (2.0, 0.5),  # actual_kw=2.0 -> error 0
        (3.0, 0.5),  # actual_kw=2.0 -> error +1.0 (supraestimare)
        (1.0, 0.5),  # actual_kw=2.0 -> error -1.0 (subestimare)
        (4.0, 1.0),  # actual_kw=4.0 -> error 0
    ]
    for t, (forecast_kw, actual_kwh) in zip(intervals, fixture, strict=True):
        _forecast(db, station, issued_at=t - timedelta(hours=1), interval_start=t, predicted_kw=forecast_kw)
        _actual(db, station, t, actual_kwh)
    db.commit()

    result = backtest.backtest_pv_forecast(db, station, intervals[0], intervals[-1] + timedelta(minutes=15))

    assert result.n_expected_intervals == 4
    assert result.n_paired == 4
    assert result.n_missing_forecast == 0
    assert result.n_missing_actual == 0
    # errors: 0, +1.0, -1.0, 0 -> MAE = (0+1+1+0)/4 = 0.5 ; bias = (0+1-1+0)/4 = 0
    assert result.mae_kw == pytest.approx(0.5)
    assert result.bias_kw == pytest.approx(0.0)


def test_backtest_excludes_forecast_issued_after_the_predicted_interval(db):
    """Aceeasi regula anti-look-ahead ca dashboard_service.get_forecast_vs_actual
    (issue #13): o prognoza "din viitor" fata de intervalul prezis nu trebuie
    folosita in backtesting -- ar da o impresie falsa de acuratete (folosind
    informatie care nu era inca disponibila la momentul prezis)."""
    station = _station(db, "lookahead")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    end = t0 + timedelta(minutes=15)

    # Prognoza emisa DUPA momentul prezis -- trebuie ignorata complet.
    _forecast(db, station, issued_at=t0 + timedelta(hours=2), interval_start=t0, predicted_kw=99.0)
    _actual(db, station, t0, 0.5)  # actual_kw = 2.0
    db.commit()

    result = backtest.backtest_pv_forecast(db, station, t0, end)

    assert result.n_expected_intervals == 1
    assert result.n_paired == 0
    assert result.n_missing_forecast == 1  # nicio prognoza VALIDA (as-of) disponibila
    assert result.mae_kw is None
    assert result.bias_kw is None


def test_backtest_picks_most_recent_valid_as_of_forecast(db):
    station = _station(db, "asof")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    end = t0 + timedelta(minutes=15)

    _forecast(db, station, issued_at=t0 - timedelta(hours=3), interval_start=t0, predicted_kw=1.0)
    _forecast(db, station, issued_at=t0 - timedelta(hours=1), interval_start=t0, predicted_kw=2.0)  # mai recenta, tot valida
    _actual(db, station, t0, 0.5)  # actual_kw = 2.0
    db.commit()

    result = backtest.backtest_pv_forecast(db, station, t0, end)

    assert result.n_paired == 1
    assert result.mae_kw == pytest.approx(0.0)  # 2.0 (cea mai recenta) - 2.0 real = 0


def test_backtest_reports_missing_forecast_separately_not_as_zero_error(db):
    station = _station(db, "missfc")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    end = t0 + timedelta(minutes=30)  # 2 intervale asteptate

    _forecast(db, station, issued_at=t0 - timedelta(hours=1), interval_start=t0, predicted_kw=2.0)
    _actual(db, station, t0, 0.5)
    # Al doilea interval (t0+15min): nicio prognoza deloc.
    _actual(db, station, t0 + timedelta(minutes=15), 0.5)
    db.commit()

    result = backtest.backtest_pv_forecast(db, station, t0, end)

    assert result.n_expected_intervals == 2
    assert result.n_paired == 1
    assert result.n_missing_forecast == 1
    assert result.mae_kw == pytest.approx(0.0)


def test_backtest_reports_missing_actual_separately_not_as_zero_error(db):
    station = _station(db, "missact")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    end = t0 + timedelta(minutes=15)

    _forecast(db, station, issued_at=t0 - timedelta(hours=1), interval_start=t0, predicted_kw=2.0)
    # Fara telemetrie deloc pentru acest interval.
    db.commit()

    result = backtest.backtest_pv_forecast(db, station, t0, end)

    assert result.n_paired == 0
    assert result.n_missing_actual == 1
    assert result.n_missing_forecast == 0
    assert result.mae_kw is None


def test_backtest_excludes_actual_with_insufficient_coverage(db):
    station = _station(db, "lowcov")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    end = t0 + timedelta(minutes=15)

    _forecast(db, station, issued_at=t0 - timedelta(hours=1), interval_start=t0, predicted_kw=2.0)
    _actual(db, station, t0, 0.5, coverage=0.5)  # sub pragul MIN_COVERAGE
    db.commit()

    result = backtest.backtest_pv_forecast(db, station, t0, end)

    assert result.n_paired == 0
    assert result.n_missing_actual == 1


def test_backtest_rejects_invalid_range(db):
    station = _station(db, "badrange")
    t0 = utcnow()
    with pytest.raises(ValueError):
        backtest.backtest_pv_forecast(db, station, t0, t0)
