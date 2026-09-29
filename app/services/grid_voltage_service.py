"""Observed grid voltage only: no interpolation, power inference or energy writes."""

import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.core.security import utcnow
from app.models.device import Device
from app.models.grid_voltage import GridVoltageSample
from app.models.telemetry import TelemetryRaw

PHASES = ("L1", "L2", "L3")
RANK = {"missing": -1, "measured": 0, "derived": 1, "stale": 2, "simulated": 3}
GRID_POINT = re.compile(r"^Grid\s*(?:Voltage\s*(L[123])|(L[123])\s*Voltage)$", re.I)


def voltage(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result < 99999999 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def formatted(value):
    return str(value.quantize(Decimal("0.001"))) if value is not None else None


def quality(flags, present=True):
    if not present:
        return "missing"
    return max((f for f in flags if f in RANK), key=RANK.get, default="measured")


def parse_deye(item):
    points = item.get("dataList")
    phases = {}
    duplicates = set()
    if not isinstance(points, list):
        return phases
    for point in points:
        if not isinstance(point, dict):
            continue
        match = GRID_POINT.fullmatch(str(point.get("key", "")))
        if not match or point.get("unit") != "V":
            continue
        phase = (match[1] or match[2]).upper()
        if phase in phases:
            duplicates.add(phase)
        phases[phase] = voltage(point.get("value"))
    for phase in duplicates:
        phases[phase] = None
    return phases


def ingest_deye(db, station_id, source_id, label, at, received_at, phases):
    if not phases:
        return
    db.execute(
        insert(GridVoltageSample)
        .values(
            id=uuid4(),
            station_id=station_id,
            source_id=source_id,
            label=label,
            measured_at=at,
            received_at=received_at,
            reported_phases=list(phases),
            **{phase.lower() + "_v": phases.get(phase) for phase in PHASES},
        )
        .on_conflict_do_nothing(constraint="uq_grid_voltage_time")
    )


def sources(db, station_id, now, cutoff):
    raw = TelemetryRaw
    native = db.execute(
        select(raw.device_id, raw.source, raw.measured_at, Device.name, Device.station_id)
        .join(Device, Device.id == raw.device_id)
        .where(
            raw.station_id == station_id,
            raw.measured_at <= now,
            raw.measured_at >= cutoff,
            raw.source != "deye_cloud",
            raw.diagnostics["phases"].as_string().is_not(None),
        )
        .distinct(raw.device_id, raw.source)
        .order_by(raw.device_id, raw.source, raw.measured_at.desc())
    )
    result = [
        {
            "id": f"device:{row.device_id}:{row.source}",
            "label": row.name if row.station_id == station_id else "Dispozitiv EMS anterior",
            "provider": row.source,
            "last_reported_at": row.measured_at,
        }
        for row in native
    ]
    cloud = GridVoltageSample
    result.extend(
        {
            "id": f"deye:{row.source_id}",
            "label": row.label,
            "provider": "deye_cloud",
            "last_reported_at": row.measured_at,
        }
        for row in db.scalars(
            select(cloud)
            .where(
                cloud.station_id == station_id,
                cloud.measured_at <= now,
                cloud.measured_at >= cutoff,
            )
            .distinct(cloud.source_id)
            .order_by(cloud.source_id, cloud.measured_at.desc())
        )
    )
    return sorted(result, key=lambda row: (row["last_reported_at"], row["id"]), reverse=True)


def observations(db, station_id, source_id, start, end, now):
    if source_id.startswith("deye:"):
        model = GridVoltageSample
        query = select(model).where(model.source_id == source_id.split(":")[1])
        query = query.where(
            model.station_id == station_id,
            model.measured_at >= start,
            model.measured_at < end,
            model.measured_at <= now,
        )
        for row in db.scalars(query.order_by(model.measured_at).execution_options(yield_per=1000)):
            yield (
                row.measured_at,
                row.received_at,
                {
                    phase: (getattr(row, phase.lower() + "_v"), set())
                    for phase in row.reported_phases
                    if phase in PHASES
                },
            )
        return
    _, device_id, provider = source_id.split(":", 2)
    raw = TelemetryRaw
    # Avoid loading full raw provider payloads and unrelated energy fields.
    query = (
        select(
            raw.measured_at,
            raw.received_at,
            raw.diagnostics,
            raw.quality_flags,
            raw.is_simulated,
            raw.is_late,
        )
        .where(
            raw.station_id == station_id,
            raw.device_id == device_id,
            raw.source == provider,
            raw.measured_at >= start,
            raw.measured_at < end,
            raw.measured_at <= now,
        )
        .order_by(raw.measured_at, raw.received_at, raw.id)
    )
    for row in db.execute(query.execution_options(yield_per=1000)):
        flags = {key for key, value in (row.quality_flags or {}).items() if value}
        if row.is_simulated:
            flags.add("simulated")
        if row.is_late:
            flags.add("late")
        phases = {}
        for phase in (row.diagnostics or {}).get("phases", []):
            name = phase.get("phase")
            if name not in PHASES or phase.get("circuit", "grid") != "grid":
                continue
            own_flags = flags | (
                {phase["quality"]} if phase.get("quality", "measured") != "measured" else set()
            )
            phases[name] = (voltage(phase.get("voltage_v")), own_flags)
        yield row.measured_at, row.received_at, phases


def history(db, station, day=None, source_id=None, now=None):
    now = now or utcnow()
    zone = ZoneInfo(station.timezone)
    today = now.astimezone(zone).date()
    cutoff = now - timedelta(days=get_settings().telemetry_raw_retention_days)
    earliest = cutoff.astimezone(zone).date()
    day = day or today
    if not earliest <= day <= today:
        raise ValueError("Alege o zi din perioada de retentie, pana astazi.")
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    available = sources(db, station.id, now, cutoff)
    if source_id and source_id not in {row["id"] for row in available}:
        raise LookupError("Sursa de tensiune nu este disponibila pentru aceasta statie.")
    source_id = source_id or (available[0]["id"] if available else None)
    minutes = int((end - start).total_seconds() // 60)
    elapsed = min(minutes, int((now - start).total_seconds() // 60) + 1)
    buckets = {phase: {} for phase in PHASES}
    latest = {}
    seen = set()
    if source_id:
        for at, received_at, phases in observations(db, station.id, source_id, start, end, now):
            seen.update(phases)
            for phase in seen:
                value, flags = phases.get(phase, (None, set()))
                latest[phase] = (at, received_at, value, flags)
                index = int((at - start).total_seconds() // 60)
                bucket = buckets[phase].setdefault(
                    index,
                    {"sum": Decimal(0), "samples": 0, "min": None, "max": None, "flags": set()},
                )
                bucket["flags"].update(flags)
                if value is None:
                    continue
                bucket["sum"] += value
                bucket["samples"] += 1
                bucket["min"] = value if bucket["min"] is None else min(value, bucket["min"])
                bucket["max"] = value if bucket["max"] is None else max(value, bucket["max"])
    result = []
    for phase in PHASES:
        points = []
        flags = set()
        for index in range(minutes):
            bucket = buckets[phase].get(index, {})
            count = bucket.get("samples", 0)
            own_flags = bucket.get("flags", set())
            flags.update(own_flags)
            points.append(
                {
                    "start": start + timedelta(minutes=index),
                    "mean_v": formatted(bucket["sum"] / count) if count else None,
                    "min_v": formatted(bucket.get("min")),
                    "max_v": formatted(bucket.get("max")),
                    "samples": count,
                    "quality": quality(own_flags, bool(count)),
                    "flags": sorted(own_flags),
                }
            )
        present = [bucket for bucket in buckets[phase].values() if bucket["samples"]]
        at, received_at, value, last_flags = latest.get(phase, (None, None, None, set()))
        last_flags = set(last_flags)
        if at is not None and now - at > timedelta(minutes=10):
            last_flags.add("stale")
        result.append(
            {
                "phase": phase,
                "latest": {
                    "value_v": formatted(value),
                    "measured_at": at,
                    "received_at": received_at,
                    "quality": quality(last_flags, value is not None),
                    "flags": sorted(last_flags),
                },
                "min_v": formatted(min(b["min"] for b in present)) if present else None,
                "max_v": formatted(max(b["max"] for b in present)) if present else None,
                "samples": sum(b["samples"] for b in present),
                "observed_minutes": len(present),
                "quality": quality(flags, bool(present)),
                "flags": sorted(flags),
                "points": points,
            }
        )
    return {
        "station_id": station.id,
        "timezone": station.timezone,
        "day": day,
        "today": today,
        "earliest_day": earliest,
        "start": start,
        "end": end,
        "generated_at": now,
        "sources": available,
        "source_id": source_id,
        "elapsed_minutes": elapsed,
        "phases": result,
    }
