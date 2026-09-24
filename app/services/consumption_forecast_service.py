"""Prognoza de consum bazata pe profil istoric (interval de 15 minute x zi a
saptamanii), calculat DOAR din date trecute (niciodata din date viitoare).

Cold start: daca statia are mai putin de MIN_DAYS_FOR_FULL_PROFILE zile de
istoric, prognoza e marcata explicit `is_cold_start=True` si foloseste un
profil mediu global (fara distinctie pe zi a saptamanii) sau, in absenta
completa a istoricului, ridica o eroare explicita -- nu se inventeaza consum.

Limitare documentata: platforma nu are o sursa de date separata pentru
"consumatori flexibili" (ex. masina de spalat, boiler controlat individual);
`flexible_component_kw` ramane 0 si tot consumul non-EV e raportat ca
`base_load_kw`. Componenta EV e separata folosind `ev_power_w`/`ev_energy_kwh`
masurate, cand dispozitivul le raporteaza.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.forecast import ConsumptionForecast
from app.models.station import Station, StationConfigVersion
from app.models.telemetry import TelemetryAggregate

MIN_DAYS_FOR_FULL_PROFILE = 7


class ConsumptionForecastError(Exception):
    pass


def _bucket_key(dt: datetime, tz: ZoneInfo) -> tuple[int, int]:
    local = dt.astimezone(tz)
    return local.weekday(), local.hour * 4 + local.minute // 15


def generate_consumption_forecast(db: Session, station: Station, horizon_start: datetime, horizon_end: datetime) -> list[ConsumptionForecast]:
    try:
        tz = ZoneInfo(station.timezone)
    except Exception:
        tz = ZoneInfo("Europe/Bucharest")

    now = utcnow()
    history = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start < now,
        )
    ).all()

    if not history:
        raise ConsumptionForecastError(
            "Fara istoric de telemetrie agregata -- nu se poate genera o prognoza de consum (cold start complet)."
        )

    config = db.scalar(select(StationConfigVersion).where(
        StationConfigVersion.station_id == station.id
    ).order_by(StationConfigVersion.version.desc()).limit(1))
    ev_disabled = config is not None and config.ev_enabled is False
    history = [a for a in history if a.load_energy_kwh is not None
               and (a.coverage or {}).get("load", 0) >= 0.9
               and (ev_disabled or (a.ev_energy_kwh is not None and (a.coverage or {}).get("ev", 0) >= 0.9))]
    if not history:
        raise ConsumptionForecastError("Istoric cu acoperire insuficienta pentru consum/EV.")
    if station.execution_mode == "live":
        history = [a for a in history if a.data_quality == "measured"]
        if not history:
            raise ConsumptionForecastError("Istoric nemasurat sau stale -- prognoza live blocata.")
    untrusted = any(a.data_quality != "measured" for a in history)
    simulated = any(a.data_quality == "simulated" for a in history)
    distinct_days = {a.period_start.astimezone(tz).date() for a in history}
    is_cold_start = len(distinct_days) < MIN_DAYS_FOR_FULL_PROFILE

    buckets: dict[tuple[int, int], list[tuple[Decimal, Decimal]]] = {}
    all_load: list[Decimal] = []
    all_ev: list[Decimal] = []
    for a in history:
        load_kw = a.load_energy_kwh * 4  # kWh/15min -> kW mediu
        ev_kw = Decimal(0) if ev_disabled else a.ev_energy_kwh * 4
        all_load.append(load_kw)
        all_ev.append(ev_kw)
        if not is_cold_start:
            key = _bucket_key(a.period_start, tz)
            buckets.setdefault(key, []).append((load_kw, ev_kw))

    global_avg_load = sum(all_load) / len(all_load)
    global_avg_ev = sum(all_ev) / len(all_ev)

    created = []
    forecast_issued_at = now
    t = horizon_start
    while t < horizon_end:
        if is_cold_start:
            load_kw, ev_kw = global_avg_load, global_avg_ev
        else:
            key = _bucket_key(t, tz)
            samples = buckets.get(key)
            if samples:
                load_kw = sum(s[0] for s in samples) / len(samples)
                ev_kw = sum(s[1] for s in samples) / len(samples)
            else:
                load_kw, ev_kw = global_avg_load, global_avg_ev

        base_kw = max(load_kw - ev_kw, Decimal(0))
        cf = ConsumptionForecast(
            station_id=station.id,
            issued_at=forecast_issued_at,
            interval_start=t,
            interval_end=t + timedelta(minutes=15),
            source="historical_profile",
            source_version=("weekday_15min_v1" if not is_cold_start else "global_average_cold_start_v1") + ("_untrusted" if untrusted else ""),
            base_load_kw=round(base_kw, 4),
            ev_component_kw=round(ev_kw, 4),
            flexible_component_kw=0,
            is_cold_start=is_cold_start,
            is_synthetic=simulated,
            confidence="low" if untrusted or is_cold_start else "nominal",
        )
        db.add(cf)
        created.append(cf)
        t += timedelta(minutes=15)

    db.flush()
    return created
