"""Capability-driven PV input observations; no station-energy reconstruction."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.security import utcnow
from app.models.solar import SolarInputAggregate, SolarInputSample, SolarInverter, SolarTracker
from app.services.aggregation_service import MAX_GAP_SECONDS
from app.services.ev_analytics_service import seconds

FIELDS = ("voltage_v", "current_a", "power_w")
UNITS = dict(zip(FIELDS, ("V", "A", "W"), strict=True))
ZERO = Decimal(0)
MAX_GAP = timedelta(seconds=MAX_GAP_SECONDS)


def quality(flags, missing=False):
    return (
        "missing"
        if missing
        else next((q for q in ("simulated", "stale", "derived") if q in flags), "measured")
    )


def ingest(
    db,
    device,
    source,
    key,
    at,
    received,
    inputs,
    *,
    flags=(),
    ac_power=None,
    ac_quality="measured",
    label=None,
    model=None,
):
    if not inputs:
        return
    statement = insert(SolarInverter).values(
        station_id=device.station_id,
        device_id=device.id,
        source=source,
        source_key=key,
        label=label or device.name,
        model_name=model,
        ac_observation={},
    )
    db.execute(statement.on_conflict_do_nothing(constraint="uq_solar_inverter_source"))
    inverter = db.scalar(
        select(SolarInverter)
        .where(
            SolarInverter.station_id == device.station_id,
            SolarInverter.device_id == device.id,
            SolarInverter.source == source,
            SolarInverter.source_key == key,
        )
        .with_for_update()
    )
    if inverter.station_id != device.station_id:
        raise ValueError("Inverter station assignment mismatch")
    at = at.astimezone(UTC)
    previous = inverter.ac_observation.get("measured_at")
    if not previous or at.isoformat() > previous:
        inverter.ac_observation = {
            "value": str(ac_power) if ac_power is not None else None,
            "unit": "W",
            "measured_at": at.isoformat(),
            "received_at": received.isoformat(),
            "flags": sorted(set(flags) | ({ac_quality} if ac_quality != "measured" else set())),
        }
    for item in inputs:
        index = item["index"]
        capabilities = item.get("supported_metrics")
        observed = [field for field in FIELDS if item.get(field) is not None]
        stmt = insert(SolarTracker).values(
            inverter_id=inverter.id,
            input_index=index,
            kind=item.get("kind", "mppt"),
            label=item.get("label")
            or f"{'MPPT' if item.get('kind', 'mppt') == 'mppt' else 'PV'} {index}",
            supported_metrics=capabilities if capabilities is not None else observed,
            last_reported_at=at,
            revision=0,
            configuration={},
        )
        db.execute(stmt.on_conflict_do_nothing(constraint="uq_solar_tracker_index"))
        tracker = db.scalar(
            select(SolarTracker).where(
                SolarTracker.inverter_id == inverter.id, SolarTracker.input_index == index
            )
        )
        if at > tracker.last_reported_at:
            tracker.supported_metrics = (
                capabilities
                if capabilities is not None
                else sorted(set(tracker.supported_metrics) | set(observed))
            )
            tracker.last_reported_at = at
        provenance = set(flags) | {item.get("quality", "measured")}
        values = {
            field: Decimal(str(item[field])) if item.get(field) is not None else None
            for field in FIELDS
        }
        db.execute(
            insert(SolarInputSample)
            .values(
                tracker_id=tracker.id,
                measured_at=at,
                received_at=received,
                **values,
                quality=quality(provenance),
                flags=sorted(provenance - {"measured"}),
            )
            .on_conflict_do_nothing(constraint="uq_solar_sample_time")
        )
    db.flush()


def ingest_device(db, device, row):
    diagnostics = row["diagnostics"]
    flags = {key for key in ("simulated", "stale", "derived") if row["quality_flags"].get(key)}
    if row["is_simulated"]:
        flags.add("simulated")
    if row["is_late"]:
        flags.add("late")
    ingest(
        db,
        device,
        "device_rs485",
        "primary",
        row["measured_at"],
        row["received_at"],
        diagnostics.get("mppt", []),
        flags=flags,
        ac_power=diagnostics.get("inverter", {}).get("ac_output_power_w"),
        ac_quality=diagnostics.get("inverter", {}).get("quality", "measured"),
    )


def snapshot(db, station, now=None):
    now = now or utcnow()
    inverters = db.scalars(
        select(SolarInverter)
        .where(SolarInverter.station_id == station.id)
        .order_by(SolarInverter.id)
    ).all()
    trackers = db.scalars(
        select(SolarTracker)
        .where(SolarTracker.inverter_id.in_([i.id for i in inverters]))
        .order_by(SolarTracker.input_index)
    ).all()
    samples = db.scalars(
        select(SolarInputSample)
        .where(SolarInputSample.tracker_id.in_([t.id for t in trackers]))
        .distinct(SolarInputSample.tracker_id)
        .order_by(SolarInputSample.tracker_id, SolarInputSample.measured_at.desc())
    ).all()
    latest = {r.tracker_id: r for r in samples}
    result = []
    for inverter in inverters:
        entries = []
        for tracker in trackers:
            if tracker.inverter_id != inverter.id:
                continue
            row = latest.get(tracker.id)
            stale = row is not None and (
                now - row.measured_at > timedelta(minutes=10)
                or row.measured_at > now + timedelta(seconds=30)
                or row.measured_at.isoformat() < inverter.ac_observation.get("measured_at", "")
            )
            flags = set(row.flags if row else []) | ({"stale"} if stale else set())
            metrics = {
                field: {
                    "value": str(getattr(row, field))
                    if row
                    and field in tracker.supported_metrics
                    and getattr(row, field) is not None
                    else None,
                    "unit": UNITS[field],
                    "supported": field in tracker.supported_metrics,
                    "quality": quality(
                        flags,
                        row is None
                        or field not in tracker.supported_metrics
                        or getattr(row, field) is None,
                    ),
                }
                for field in FIELDS
            }
            entries.append(
                {
                    "id": str(tracker.id),
                    "index": tracker.input_index,
                    "label": tracker.label,
                    "kind": tracker.kind,
                    "source": inverter.source,
                    "capability": "reported",
                    "metrics": metrics,
                    "flags": sorted(flags),
                    "measured_at": row.measured_at.isoformat() if row else None,
                    "received_at": row.received_at.isoformat() if row else None,
                    "freshness": "missing"
                    if row is None
                    else "stale"
                    if "stale" in flags
                    else "fresh",
                    "revision": tracker.revision,
                    "configuration": tracker.configuration,
                }
            )
        values = [e["metrics"]["power_w"]["value"] for e in entries]
        dc_total = (
            sum((Decimal(v) for v in values), ZERO)
            if values
            and all(v is not None for v in values)
            and all(e["freshness"] == "fresh" for e in entries)
            else None
        )
        ac = dict(inverter.ac_observation)
        ac_flags = set(ac.get("flags", []))
        ac_at = datetime.fromisoformat(ac["measured_at"]) if ac.get("measured_at") else None
        if ac_at and (now - ac_at > timedelta(minutes=10) or ac_at > now + timedelta(seconds=30)):
            ac_flags.add("stale")
        ac.update(flags=sorted(ac_flags), quality=quality(ac_flags, ac.get("value") is None))
        result.append(
            {
                "id": str(inverter.id),
                "label": inverter.label,
                "model": inverter.model_name,
                "source": inverter.source,
                "inputs": entries,
                "dc_total_w": str(dc_total) if dc_total is not None else None,
                "dc_quality": quality({f for e in entries for f in e["flags"]}, dc_total is None),
                "ac_output": ac,
                "warnings": comparison_warnings(entries),
            }
        )
    return {
        "schema_version": 1,
        "station_id": str(station.id),
        "timezone": station.timezone,
        "generated_at": now.isoformat(),
        "inverters": result,
        "dc_ac_note": "Suma intrarilor DC si iesirea AC sunt marimi distincte; bateria, pierderile si momentele de masurare pot diferi.",
    }


def comparison_warnings(entries):
    groups = defaultdict(list)
    for entry in entries:
        config = entry["configuration"]
        if config.get("comparison_group"):
            groups[config["comparison_group"]].append(entry)
    warnings = []
    for name, group in groups.items():
        if len(group) < 2 or any(
            e["freshness"] != "fresh" or e["metrics"]["power_w"]["quality"] != "measured"
            for e in group
        ):
            continue
        configs = [e["configuration"] for e in group]
        if any(
            any(c.get(k) is None for k in ("installed_kw", "azimuth_deg", "tilt_deg"))
            for c in configs
        ):
            continue
        angles = {(Decimal(c["azimuth_deg"]), Decimal(c["tilt_deg"])) for c in configs}
        if len(angles) != 1:
            continue
        values = [
            Decimal(e["metrics"]["power_w"]["value"]) / Decimal(e["configuration"]["installed_kw"])
            for e in group
        ]
        maximum = max(values)
        if maximum < 100:  # W/kWp: suppress darkness and very low irradiance.
            continue
        threshold = max(Decimal(c["warning_threshold_percent"]) for c in configs)
        delta = (maximum - min(values)) / maximum * 100
        if delta > threshold:
            warnings.append(
                {
                    "group": name,
                    "code": "input_imbalance",
                    "difference_percent": str(delta.quantize(Decimal("0.1"))),
                    "threshold_percent": str(threshold),
                    "message": "Diferenta intre intrari comparabile. Verifica umbrirea si conexiunile; o singura observatie nu stabileste cauza.",
                }
            )
    return warnings


def integrate(rows, start, end):
    values, coverage, flags = {}, {}, set()
    duration = seconds(end - start)
    energy = None
    for field in FIELDS:
        covered, weighted = ZERO, ZERO
        for index, row in enumerate(rows):
            stop = min(
                row.measured_at + MAX_GAP,
                rows[index + 1].measured_at if index + 1 < len(rows) else end,
                end,
            )
            left = max(start, row.measured_at)
            value = getattr(row, field)
            if stop <= left or value is None:
                continue
            width = seconds(stop - left)
            covered += width
            weighted += value * width
            flags.update(row.flags)
        values[field] = weighted / covered if covered else None
        coverage[field] = str(covered / duration)
        if field == "power_w" and covered:
            energy = weighted / Decimal(3600000)
    return {
        **values,
        "energy_kwh": energy,
        "coverage": coverage,
        "flags": sorted(flags),
        "quality": quality(flags, all(v is None for v in values.values())),
    }


def reaggregate(db, station, start, end):
    from app.models.station import Station

    db.execute(
        select(Station.id).where(Station.id == station.id).with_for_update(key_share=True)
    ).scalar_one()
    start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    rows = db.scalars(
        select(SolarInputSample)
        .join(SolarTracker)
        .join(SolarInverter)
        .where(
            SolarInverter.station_id == station.id,
            SolarInputSample.measured_at >= start - MAX_GAP,
            SolarInputSample.measured_at < end,
        )
        .order_by(SolarInputSample.measured_at)
    ).all()
    groups = defaultdict(list)
    for row in rows:
        groups[row.tracker_id].append(row)
    for tracker_id, samples in groups.items():
        for period, width in (("15m", 15), ("1h", 60)):
            at = start
            while at < end:
                finish = at + timedelta(minutes=width)
                data = integrate(samples, at, finish)
                statement = insert(SolarInputAggregate).values(
                    tracker_id=tracker_id,
                    period_type=period,
                    period_start=at,
                    period_end=finish,
                    **data,
                )
                db.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_solar_aggregate_period",
                        set_={**data, "updated_at": utcnow()},
                    )
                )
                at = finish


def history(db, station, tracker_id, range_key="24h", now=None):
    tracker = db.scalar(
        select(SolarTracker)
        .join(SolarInverter)
        .where(SolarInverter.station_id == station.id, SolarTracker.id == tracker_id)
    )
    if tracker is None:
        raise LookupError("Intrare inexistenta.")
    end = now or utcnow()
    start = end - timedelta(days={"24h": 1, "7d": 7, "30d": 30}[range_key])
    resolution = "15m" if range_key == "24h" else "1h"
    width = timedelta(minutes=15 if resolution == "15m" else 60)
    start = start.replace(
        minute=start.minute
        // (15 if resolution == "15m" else 60)
        * (15 if resolution == "15m" else 60),
        second=0,
        microsecond=0,
    )
    rows = db.scalars(
        select(SolarInputAggregate)
        .where(
            SolarInputAggregate.tracker_id == tracker.id,
            SolarInputAggregate.period_type == resolution,
            SolarInputAggregate.period_start >= start,
            SolarInputAggregate.period_start < end,
        )
        .order_by(SolarInputAggregate.period_start)
        .limit(721)
    ).all()
    by_start = {row.period_start: row for row in rows}
    points = []
    at = start
    while at < end:
        row = by_start.get(at)
        points.append(
            {
                "start": at.isoformat(),
                "end": (at + width).isoformat(),
                **{
                    field: str(getattr(row, field))
                    if row and getattr(row, field) is not None
                    else None
                    for field in FIELDS
                },
                "energy_kwh": str(row.energy_kwh) if row and row.energy_kwh is not None else None,
                "coverage": row.coverage if row else dict.fromkeys(FIELDS, "0"),
                "quality": row.quality if row else "missing",
                "flags": row.flags if row else [],
            }
        )
        at += width
    return {
        "schema_version": 1,
        "tracker_id": str(tracker.id),
        "timezone": station.timezone,
        "resolution": resolution,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "units": UNITS,
        "aggregation": "time_weighted_mean",
        "points": points,
    }
