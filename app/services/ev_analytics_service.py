from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import select

from app.models.ev import EVSE, ChargingSession, EVConnector, EVObservation, Vehicle
from app.models.market import ImportRun, MarketPriceInterval
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryRaw
from app.services.tariff_service import (
    compute_effective_price_lei_per_kwh,
    get_current_tariff_version,
)

ZERO = Decimal(0)


def seconds(delta):
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000


def energy_segments(db, session):
    observations = db.scalars(select(EVObservation).where(
        EVObservation.session_id == session.id, EVObservation.applied.is_(True),
    ).order_by(EVObservation.observed_at, EVObservation.id)).all()
    segments, reasons = [], set()
    full = len(observations) >= 2
    qualities = set()
    for before, after in pairwise(observations):
        duration = seconds(after.observed_at - before.observed_at)
        if duration <= 0:
            continue
        quality = next((q for q in ("simulated", "stale", "estimated") if q in (before.quality, after.quality)), "measured")
        qualities.add(quality)
        energy, method = None, "unavailable"
        if before.meter_kwh is not None and after.meter_kwh is not None:
            if before.meter_epoch == after.meter_epoch and after.meter_kwh >= before.meter_kwh:
                energy, method = after.meter_kwh - before.meter_kwh, "meter_delta"
            else:
                reasons.add("meter_reset")
        elif before.power_kw is not None and duration <= 300:
            energy, method = before.power_kw * duration / 3600, "power_integral"
            if quality == "measured":
                quality = "estimated"
                qualities.add(quality)
        if energy is None:
            reasons.add("missing_energy_segment")
            full = False
        segments.append({"start": before.observed_at, "end": after.observed_at,
                         "energy_kwh": energy, "quality": quality, "method": method})
    total = sum((s["energy_kwh"] for s in segments if s["energy_kwh"] is not None), ZERO)
    quality = next((q for q in ("simulated", "stale", "estimated") if q in qualities), "measured") if segments else "unknown"
    return {
        "segments": segments, "total_energy_kwh": total if full else None,
        "known_energy_kwh": total if any(s["energy_kwh"] is not None for s in segments) else None,
        "quality": quality, "reason_codes": sorted(reasons), "observations": observations,
    }


def price_at(db, station_id, at, direction="import"):
    tariff = get_current_tariff_version(db, station_id, direction, at)
    market = db.scalar(select(MarketPriceInterval).join(ImportRun).where(
        MarketPriceInterval.is_current.is_(True), ImportRun.is_synthetic_fixture.is_(False),
        MarketPriceInterval.interval_start <= at, MarketPriceInterval.interval_end > at,
    ))
    return compute_effective_price_lei_per_kwh(tariff, market.price_lei_per_kwh if market else None)


def split_prices(db, station_id, start, end):
    boundaries = {start, end}
    for version in db.scalars(select(TariffVersion).join(Tariff).where(
        Tariff.station_id == station_id, Tariff.direction == "import", TariffVersion.valid_from < end,
        (TariffVersion.valid_to.is_(None)) | (TariffVersion.valid_to > start),
    )):
        for instant in (version.valid_from, version.valid_to):
            if instant and start < instant < end:
                boundaries.add(instant)
    for market in db.scalars(select(MarketPriceInterval).where(
        MarketPriceInterval.is_current.is_(True), MarketPriceInterval.interval_start < end,
        MarketPriceInterval.interval_end > start,
    )):
        boundaries.update(t for t in (market.interval_start, market.interval_end) if start < t < end)
    boundaries = sorted(boundaries)
    return [(a, b, price_at(db, station_id, a)) for a, b in pairwise(boundaries)]


def cost_segments(db, station_id, segments):
    cost, priced_energy, known_energy = ZERO, ZERO, ZERO
    for segment in segments:
        energy = segment["energy_kwh"]
        if energy is None or segment["quality"] not in ("measured", "estimated"):
            continue
        known_energy += energy
        duration = seconds(segment["end"] - segment["start"])
        parts = split_prices(db, station_id, segment["start"], segment["end"])
        allocated = ZERO
        for index, (start, end, price) in enumerate(parts):
            portion = energy - allocated if index == len(parts) - 1 else energy * seconds(end - start) / duration
            allocated += portion
            if price is not None:
                cost += portion * price
                priced_energy += portion
    return cost, priced_energy, known_energy


def source_estimate(db, session, segments):
    allocated = {"pv_kwh": ZERO, "battery_kwh": ZERO, "grid_kwh": ZERO}
    covered_energy, total_energy = ZERO, ZERO
    if not segments:
        return {"values": None, "coverage": ZERO, "confidence": "unavailable"}
    rows = db.scalars(select(TelemetryRaw).where(
        TelemetryRaw.station_id == session.station_id,
        TelemetryRaw.measured_at >= session.started_at - timedelta(minutes=5),
        TelemetryRaw.measured_at < segments[-1]["end"],
    ).order_by(TelemetryRaw.measured_at)).all()
    for segment in segments:
        if segment["energy_kwh"] is None:
            continue
        total_energy += segment["energy_kwh"]
        if segment["quality"] not in ("measured", "estimated"):
            continue
        for index, row in enumerate(rows):
            start = max(segment["start"], row.measured_at)
            end = min(segment["end"], row.measured_at + timedelta(minutes=5),
                      rows[index + 1].measured_at if index + 1 < len(rows) else segment["end"])
            if end <= start or row.is_simulated or any(row.quality_flags.get(k) for k in ("simulated", "stale", "derived")):
                continue
            if any(v is None for v in (row.pv_power_w, row.grid_power_w, row.battery_power_w)):
                continue
            supplies = (max(row.pv_power_w, ZERO), max(-row.battery_power_w, ZERO), max(row.grid_power_w, ZERO))
            total = sum(supplies)
            if total <= 0:
                continue
            energy = segment["energy_kwh"] * seconds(end - start) / seconds(segment["end"] - segment["start"])
            covered_energy += energy
            for key, power in zip(allocated, supplies, strict=True):
                allocated[key] += energy * power / total
    return {
        "values": allocated if covered_energy > 0 else None,
        "coverage": covered_energy / total_energy if total_energy > 0 else ZERO,
        "confidence": "low" if covered_energy > 0 else "unavailable",
        "method": "Proportional AC bus supply, with uniform allocation between EV meter readings; estimated, not physical tracing.",
    }


def session_summary(db, session):
    result = energy_segments(db, session)
    observations, segments = result.pop("observations"), result.pop("segments")
    cost, priced, known = cost_segments(db, session.station_id, segments)
    duration = seconds((session.ended_at or (observations[-1].observed_at if observations else session.started_at)) - session.started_at)
    covered_seconds = sum((seconds(s["end"] - s["start"]) for s in segments if s["energy_kwh"] is not None), ZERO)
    complete = result["total_energy_kwh"] is not None and result["quality"] in ("measured", "estimated")
    all_priced = complete and priced == result["total_energy_kwh"]
    vehicle = db.get(Vehicle, session.vehicle_id) if session.vehicle_id else None
    average = cost / priced if all_priced and priced > 0 else None
    connector = db.get(EVConnector, session.connector_id)
    evse = db.get(EVSE, connector.evse_id)
    baseline_cost = None
    if complete and evse.max_power_kw is not None and evse.max_power_kw > 0:
        end = session.started_at + timedelta(microseconds=int(result["total_energy_kwh"] / evse.max_power_kw * 3600000000))
        if end > session.started_at:
            baseline, baseline_priced, _ = cost_segments(db, session.station_id, [{
                "start": session.started_at, "end": end, "energy_kwh": result["total_energy_kwh"], "quality": "estimated",
            }])
            if baseline_priced == result["total_energy_kwh"]:
                baseline_cost = baseline
    return {
        **result, "session": session,
        "energy_coverage": covered_seconds / duration if duration > 0 else ZERO,
        "peak_power_kw": max((o.power_kw for o in observations if o.power_kw is not None and o.quality == "measured"), default=None),
        "estimated_cost_lei": cost if all_priced else None,
        "known_cost_lei": cost if priced > 0 else None,
        "priced_energy_kwh": priced, "price_coverage": priced / known if known > 0 else None,
        "average_cost_lei_kwh": average,
        "cost_100km_lei": average * vehicle.consumption_kwh_100km if average is not None and vehicle and vehicle.consumption_kwh_100km is not None else None,
        "cost_method": "Energie EV x tarif istoric de import; estimare echivalent retea, fara taxe fixe sau cost de uzura. Nu este factura si nu scade energia PV.",
        "source_estimate": source_estimate(db, session, segments),
        "baseline_cost_lei": baseline_cost,
        "baseline_method": "Aceeasi energie, incarcare imediata la puterea maxima declarata, integral la tariful de import; fara constrangeri viitoare presupuse.",
        "timing_savings_estimate_lei": baseline_cost - cost if baseline_cost is not None and all_priced else None,
        "optimization_savings_lei": None,
        "optimization_attribution_reason": "Lipseste o executie EV verificata; diferenta fata de baseline nu este atribuita automat optimizarii.",
    }


def health_findings(db, station, at, historical=False):
    from app.services.ev_service import upcoming_requirements
    from app.services.health_service import Finding

    for connector, evse in db.execute(select(EVConnector, EVSE).join(EVSE).where(
        EVSE.station_id == station.id, EVSE.is_active.is_(True),
    )):
        evidence = {"connector_id": str(connector.id)}
        if historical:
            for code in ("ev_offline", "ev_fault", "ev_target_impossible", "ev_unplugged", "ev_interrupted", "ev_cost_limit"):
                yield Finding(code, None, {**evidence, "reason": "historical_schedule_unavailable"}, subject=str(connector.id))
            continue
        online = evse.last_seen_at is not None and timedelta(0) <= at - evse.last_seen_at <= timedelta(minutes=10)
        latest = db.scalar(select(EVObservation).where(EVObservation.connector_id == connector.id,
                           EVObservation.applied.is_(True), EVObservation.observed_at <= at)
                           .order_by(EVObservation.observed_at.desc()).limit(1))
        trusted_state = online and latest and latest.quality == "measured" and at - latest.observed_at <= timedelta(minutes=10)
        yield Finding("ev_offline", not online if evse.device_id else None, evidence, subject=str(connector.id))
        yield Finding("ev_fault", connector.state == "faulted" if trusted_state else None, evidence, subject=str(connector.id))
        upcoming = upcoming_requirements(db, station, connector, at)
        requirement, deadline = upcoming[0] if upcoming else (None, None)
        session = db.scalar(select(ChargingSession).where(
            ChargingSession.connector_id == connector.id, ChargingSession.started_at <= at,
        ).order_by(ChargingSession.started_at.desc()).limit(1))
        # Only an ongoing session can contribute to a future departure. Reusing a
        # completed session would count yesterday's charge against today's target.
        usable = session and session.ended_at is None and session.quality in ("measured", "estimated") and session.energy_kwh is not None
        remaining = max(requirement.minimum_energy_kwh - session.energy_kwh, ZERO) if requirement and usable else None
        possible = evse.max_power_kw * seconds(deadline - at) / 3600 if requirement and evse.max_power_kw is not None else None
        values = {**evidence, "remaining_kwh": str(remaining) if remaining is not None else None,
                  "deadline": deadline.isoformat() if deadline else None}
        yield Finding("ev_target_impossible", remaining > possible if remaining is not None and possible is not None else None,
                      values, subject=str(connector.id))
        unplugged = connector.state in ("disconnected", "available") and requirement.minimum_energy_kwh > 0 if requirement and trusted_state else None
        yield Finding("ev_unplugged", unplugged,
                      values, subject=str(connector.id))
        yield Finding("ev_interrupted", connector.state == "paused" and remaining > 0 if remaining is not None and trusted_state else None,
                      values, subject=str(connector.id))
        cost = session_summary(db, session)["estimated_cost_lei"] if usable and requirement and requirement.max_cost_lei is not None else None
        yield Finding("ev_cost_limit", cost > requirement.max_cost_lei if cost is not None else None,
                      {**evidence, "estimated_cost_lei": str(cost) if cost is not None else None}, subject=str(connector.id))
