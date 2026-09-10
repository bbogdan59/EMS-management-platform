"""Agregare energetica idempotenta: telemetrie bruta (telemetry_raw) ->
agregate pe interval de 15 minute, ora, zi si luna (telemetry_aggregates).

Idempotenta: fiecare rulare foloseste UPSERT (ON CONFLICT ... DO UPDATE) pe
constrangerea unica (station_id, period_type, period_start), deci rularea
repetata pentru acelasi interval produce acelasi rezultat, nu duplicate.

Un interval fara nicio proba de telemetrie NU produce un rand cu valori 0 --
ramane pur si simplu absent (valorile lipsa nu devin zero, cf. cerintei).

Calitatea datelor (`data_quality`) e derivata: 'simulated' daca orice proba
provine din simulator, 'estimated' daca numarul de esantioane e sub pragul
minim asteptat pentru interval (date rare/intarziate), altfel 'measured'.
Prioritate la agregare (cea mai "slaba" calitate castiga): simulated > estimated > measured.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.telemetry import TelemetryAggregate, TelemetryRaw

QUALITY_RANK = {"measured": 0, "estimated": 1, "simulated": 2, "stale": 3, "missing": 4}
MIN_SAMPLES_FOR_MEASURED_15M = 2


def _worst_quality(qualities: list[str]) -> str:
    return max(qualities, key=lambda q: QUALITY_RANK.get(q, 0)) if qualities else "missing"


def aggregate_interval_15m(db: Session, station_id: uuid.UUID, period_start: datetime) -> TelemetryAggregate | None:
    period_end = period_start + timedelta(minutes=15)
    rows = db.scalars(
        select(TelemetryRaw).where(
            TelemetryRaw.station_id == station_id,
            TelemetryRaw.measured_at >= period_start,
            TelemetryRaw.measured_at < period_end,
        )
    ).all()
    if not rows:
        return None

    duration_h = 0.25
    n = len(rows)

    def avg(getter):
        values = [float(getter(r)) for r in rows if getter(r) is not None]
        return sum(values) / len(values) if values else 0.0

    pv_kwh = avg(lambda r: r.pv_power_w) / 1000 * duration_h
    load_kwh = avg(lambda r: r.load_power_w) / 1000 * duration_h
    batt_charge_kwh = avg(lambda r: max(float(r.battery_power_w), 0) if r.battery_power_w is not None else None) / 1000 * duration_h
    batt_discharge_kwh = avg(lambda r: max(-float(r.battery_power_w), 0) if r.battery_power_w is not None else None) / 1000 * duration_h
    grid_import_kwh = avg(lambda r: max(float(r.grid_power_w), 0) if r.grid_power_w is not None else None) / 1000 * duration_h
    grid_export_kwh = avg(lambda r: max(-float(r.grid_power_w), 0) if r.grid_power_w is not None else None) / 1000 * duration_h
    ev_kwh = avg(lambda r: r.ev_power_w) / 1000 * duration_h
    avg_soc = avg(lambda r: r.battery_soc_percent)

    if any(r.is_simulated for r in rows):
        quality = "simulated"
    elif n < MIN_SAMPLES_FOR_MEASURED_15M:
        quality = "estimated"
    else:
        quality = "measured"

    values = {
        "id": uuid.uuid4(),
        "station_id": station_id,
        "period_type": "interval_15m",
        "period_start": period_start,
        "period_end": period_end,
        "pv_energy_kwh": round(pv_kwh, 6),
        "load_energy_kwh": round(load_kwh, 6),
        "battery_charge_energy_kwh": round(batt_charge_kwh, 6),
        "battery_discharge_energy_kwh": round(batt_discharge_kwh, 6),
        "grid_import_energy_kwh": round(grid_import_kwh, 6),
        "grid_export_energy_kwh": round(grid_export_kwh, 6),
        "ev_energy_kwh": round(ev_kwh, 6),
        "avg_battery_soc_percent": round(avg_soc, 2) if avg_soc else None,
        "sample_count": n,
        "data_quality": quality,
    }
    _upsert_aggregate(db, values)
    return values


def _rollup(db: Session, station_id: uuid.UUID, source_period_type: str, target_period_type: str, period_start: datetime, period_end: datetime) -> dict | None:
    rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station_id,
            TelemetryAggregate.period_type == source_period_type,
            TelemetryAggregate.period_start >= period_start,
            TelemetryAggregate.period_start < period_end,
        )
    ).all()
    if not rows:
        return None

    def total(attr):
        return sum(float(getattr(r, attr)) for r in rows)

    total_samples = sum(r.sample_count for r in rows)
    weighted_soc = sum(float(r.avg_battery_soc_percent or 0) * r.sample_count for r in rows)

    values = {
        "id": uuid.uuid4(),
        "station_id": station_id,
        "period_type": target_period_type,
        "period_start": period_start,
        "period_end": period_end,
        "pv_energy_kwh": round(total("pv_energy_kwh"), 6),
        "load_energy_kwh": round(total("load_energy_kwh"), 6),
        "battery_charge_energy_kwh": round(total("battery_charge_energy_kwh"), 6),
        "battery_discharge_energy_kwh": round(total("battery_discharge_energy_kwh"), 6),
        "grid_import_energy_kwh": round(total("grid_import_energy_kwh"), 6),
        "grid_export_energy_kwh": round(total("grid_export_energy_kwh"), 6),
        "ev_energy_kwh": round(total("ev_energy_kwh"), 6),
        "avg_battery_soc_percent": round(weighted_soc / total_samples, 2) if total_samples else None,
        "sample_count": total_samples,
        "data_quality": _worst_quality([r.data_quality for r in rows]),
    }
    _upsert_aggregate(db, values)
    return values


def aggregate_hour(db: Session, station_id: uuid.UUID, hour_start: datetime) -> dict | None:
    return _rollup(db, station_id, "interval_15m", "hour", hour_start, hour_start + timedelta(hours=1))


def aggregate_day(db: Session, station_id: uuid.UUID, day_start: datetime) -> dict | None:
    return _rollup(db, station_id, "hour", "day", day_start, day_start + timedelta(days=1))


def aggregate_month(db: Session, station_id: uuid.UUID, month_start: datetime) -> dict | None:
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return _rollup(db, station_id, "day", "month", month_start, next_month)


def _upsert_aggregate(db: Session, values: dict) -> None:
    stmt = pg_insert(TelemetryAggregate).values(**values)
    update_cols = {
        k: stmt.excluded[k]
        for k in values
        if k not in ("id", "station_id", "period_type", "period_start")
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["station_id", "period_type", "period_start"], set_=update_cols
    )
    db.execute(stmt)
