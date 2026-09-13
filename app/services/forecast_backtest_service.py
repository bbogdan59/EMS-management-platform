"""Backtesting simplu (MAE/bias) al prognozei PV pentru o statie, pe un
interval dat -- issue #53, criteriul "Backtesting MAE/bias pe statie si
interval, plus teste deterministe cu fixtures".

Foloseste ACEEASI regula "as-of" ca `dashboard_service.get_forecast_vs_actual`
(vezi acolo, fix aplicat in issue #13): pentru fiecare `interval_start`, se
foloseste doar cea mai recenta prognoza care exista deja LA MOMENTUL acelui
interval (`issued_at <= interval_start`) -- niciodata o prognoza regenerata
ulterior, "din viitor" fata de intervalul prezis. Regula e reimplementata aici
(nu importata din `dashboard_service`) ca sa nu cuplam un modul de raportare
istorica (dashboard) de un modul de evaluare a calitatii prognozei
(backtesting) -- cele doua au motive de schimbare diferite.

Scop deliberat minimal: MAE (eroare medie absoluta) si bias (eroare medie
semnata: prognoza - real; pozitiv = prognoza supraestimeaza) pe puterea PV
(kW), comparate cu telemetria agregata la 15 minute. NU calculeaza inca
metrici pe pret/consum, nu segmenteaza pe conditii meteo (senin/inorat) si nu
produce un raport vizual -- vezi `docs/LIMITATIONS.md` pentru ce ramane
explicit in afara acestui PR."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.forecast import PvForecast
from app.models.station import Station
from app.models.telemetry import TelemetryAggregate

INTERVAL = timedelta(minutes=15)
MIN_COVERAGE = 0.9


@dataclass(frozen=True)
class ForecastBacktestResult:
    station_id: uuid.UUID
    start: datetime
    end: datetime
    n_expected_intervals: int
    n_paired: int
    n_missing_forecast: int
    n_missing_actual: int
    mae_kw: float | None
    bias_kw: float | None

    def as_dict(self) -> dict:
        return {
            "station_id": str(self.station_id),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "n_expected_intervals": self.n_expected_intervals,
            "n_paired": self.n_paired,
            "n_missing_forecast": self.n_missing_forecast,
            "n_missing_actual": self.n_missing_actual,
            "mae_kw": self.mae_kw,
            "bias_kw": self.bias_kw,
        }


def _select_as_of_forecasts(rows: list[PvForecast]) -> dict[datetime, PvForecast]:
    """Pentru fiecare `interval_start`, pastreaza doar cea mai recenta
    prognoza a carei `issued_at` nu e ulterioara intervalului prezis --
    aceeasi regula anti-look-ahead ca `dashboard_service.get_forecast_vs_actual`."""
    best_by_start: dict[datetime, PvForecast] = {}
    for f in rows:
        if f.issued_at > f.interval_start:
            continue
        existing = best_by_start.get(f.interval_start)
        if existing is None or f.issued_at > existing.issued_at:
            best_by_start[f.interval_start] = f
    return best_by_start


def backtest_pv_forecast(db: Session, station: Station, start: datetime, end: datetime) -> ForecastBacktestResult:
    """MAE/bias intre prognoza PV "asa cum era cunoscuta la momentul
    respectiv" si productia PV realmente masurata, pe grila de 15 minute
    [start, end).

    Un interval fara nicio prognoza validă (as-of) e raportat separat in
    `n_missing_forecast`, NU tratat ca eroare zero. Un interval cu prognoza
    dar fara telemetrie masurata suficient de acoperita
    (`coverage['pv'] >= MIN_COVERAGE`) e raportat in `n_missing_actual`.
    Niciunul dintre cele doua nu intra in calculul MAE/bias."""
    if end <= start:
        raise ValueError("Intervalul de backtesting trebuie sa aiba end > start.")
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Limitele intervalului trebuie sa fie timezone-aware.")
    if any(
        value.minute % 15 or value.second or value.microsecond
        for value in (start, end)
    ):
        raise ValueError("Limitele intervalului trebuie aliniate la grila de 15 minute.")

    n_expected = int((end - start) / INTERVAL)

    forecast_rows = db.scalars(
        select(PvForecast).where(
            PvForecast.station_id == station.id,
            PvForecast.scenario == "expected",
            PvForecast.interval_start >= start,
            PvForecast.interval_start < end,
        )
    ).all()
    forecast_by_start = _select_as_of_forecasts(forecast_rows)

    actual_rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
    ).all()
    actual_by_start = {a.period_start: a for a in actual_rows}

    n_missing_forecast = n_expected - len(forecast_by_start)
    n_missing_actual = 0
    errors: list[float] = []
    for interval_start, forecast in forecast_by_start.items():
        actual = actual_by_start.get(interval_start)
        coverage = (actual.coverage or {}).get("pv", 0) if actual is not None else 0
        if actual is None or actual.pv_energy_kwh is None or coverage < MIN_COVERAGE:
            n_missing_actual += 1
            continue
        actual_kw = float(actual.pv_energy_kwh) * 4  # kWh/15min -> kW mediu
        forecast_kw = float(forecast.predicted_power_kw)
        errors.append(forecast_kw - actual_kw)

    if errors:
        mae_kw = round(sum(abs(e) for e in errors) / len(errors), 4)
        bias_kw = round(sum(errors) / len(errors), 4)
    else:
        mae_kw = None
        bias_kw = None

    return ForecastBacktestResult(
        station_id=station.id,
        start=start,
        end=end,
        n_expected_intervals=n_expected,
        n_paired=len(errors),
        n_missing_forecast=n_missing_forecast,
        n_missing_actual=n_missing_actual,
        mae_kw=mae_kw,
        bias_kw=bias_kw,
    )
