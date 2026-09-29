"""Deterministic, immutable day facts; optional delivery uses the existing outbox."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.security import utcnow
from app.models.forecast import PvForecast, WeatherForecast
from app.models.notification import Notification, NotificationPreference
from app.models.optimization import Plan
from app.models.station import Station
from app.models.telemetry import TelemetryAggregate
from app.services import notification_service as notifications
from app.services.ev_analytics_service import seconds
from app.services.solar_geometry_service import sun_window_for_local_date
from app.services.station_notification_service import day_bounds

VERSION = "morning-energy/v1"
MAX_AGE = timedelta(hours=6)


def season_baseline(db, station, day):
    start, _ = day_bounds(station, day)
    rows = db.scalars(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "day",
            TelemetryAggregate.period_start >= start - timedelta(days=3 * 366),
            TelemetryAggregate.period_end <= start,
            TelemetryAggregate.data_quality == "measured",
            TelemetryAggregate.pv_energy_kwh.is_not(None),
        )
    ).all()
    target = date(2000, day.month, day.day).timetuple().tm_yday
    eligible = []
    for row in rows:
        local = row.period_start.astimezone(ZoneInfo(station.timezone)).date()
        distance = abs(date(2000, local.month, local.day).timetuple().tm_yday - target)
        if (
            min(distance, 366 - distance) <= 21
            and Decimal(str((row.coverage or {}).get("pv", 0))) >= 1
        ):
            eligible.append(row)
    return {
        "method": "median-seasonal-21-days-3-years/v1",
        "days": len(eligible),
        "value_kwh": str(median([r.pv_energy_kwh for r in eligible]))
        if len(eligible) >= 7
        else None,
        "aggregate_ids": [str(r.id) for r in eligible],
    }


def forecast_facts(db, station, now):
    day = now.astimezone(ZoneInfo(station.timezone)).date()
    start, end = day_bounds(station, day)
    rows = db.scalars(
        select(PvForecast)
        .where(
            PvForecast.station_id == station.id,
            PvForecast.interval_start < end,
            PvForecast.interval_end > start,
            PvForecast.issued_at <= now,
            PvForecast.issued_at >= now - MAX_AGE,
        )
        .order_by(PvForecast.interval_start)
    ).all()
    issued = max((r.issued_at for r in rows if r.scenario == "expected"), default=None)
    if issued is None:
        return None
    batch = [r for r in rows if r.issued_at == issued]
    sun = None
    if station.latitude is not None and station.longitude is not None:
        sun = sun_window_for_local_date(
            float(station.latitude), float(station.longitude), ZoneInfo(station.timezone), day
        )

    def night_gap(left, right):
        if left == right:
            return True
        return bool(left < right and sun and (right <= sun.sunrise_utc or left >= sun.sunset_utc))

    weather = {
        r.id: r
        for r in db.scalars(
            select(WeatherForecast).where(
                WeatherForecast.id.in_(
                    [
                        r.based_on_weather_forecast_id
                        for r in batch
                        if r.based_on_weather_forecast_id
                    ]
                )
            )
        )
    }

    def total(scenario):
        subset = [r for r in batch if r.scenario == scenario]
        cursor, energy = start, Decimal(0)
        for row in subset:
            left, right = max(start, row.interval_start), min(end, row.interval_end)
            w = weather.get(row.based_on_weather_forecast_id)
            if (
                not night_gap(cursor, left)
                or right <= left
                or row.predicted_power_kw < 0
                or row.is_synthetic
                or row.confidence not in ("nominal", "medium", "high")
                or w is None
                or w.is_synthetic
                or w.confidence not in ("nominal", "medium", "high")
                or not now - MAX_AGE <= w.issued_at <= now
            ):
                return None
            energy += row.predicted_power_kw * seconds(right - left) / 3600
            cursor = right
        return energy if subset and night_gap(cursor, end) else None

    expected = total("expected")
    if expected is None:
        return None
    expected_rows = [r for r in batch if r.scenario == "expected"]
    future = [r for r in expected_rows if r.interval_start >= now and r.predicted_power_kw > 0]
    peak = max(future, key=lambda r: r.predicted_power_kw, default=None)
    baseline = season_baseline(db, station, day)
    normal = Decimal(baseline["value_kwh"]) if baseline["value_kwh"] is not None else None
    ratio = expected / normal if normal is not None and normal > 0 else None
    kind = (
        "high"
        if ratio is not None and ratio >= Decimal("1.25")
        else "low"
        if ratio is not None and ratio <= Decimal("0.75")
        else "normal"
    )
    display = str(expected.quantize(Decimal("0.1")))
    titles = {
        "high": "O zi cu productie peste obiceiul sezonului",
        "low": "O zi cu productie solara redusa",
        "normal": "Energia ta solara de astazi",
    }
    body = f"Aproximativ {display} kWh estimati astazi."
    if normal is not None:
        body += f" Reperul sezonier este {normal.quantize(Decimal('0.1'))} kWh, din {baseline['days']} zile complete."
    action = "Consulta prognoza si planul zilei inainte de a programa consumatorii."
    if peak:
        local = peak.interval_start.astimezone(ZoneInfo(station.timezone))
        action = f"Poti muta consumatorii flexibili in jurul orei {local:%H:%M}, cand prognoza solara este cea mai mare."
    plan = db.scalar(
        select(Plan)
        .where(
            Plan.station_id == station.id,
            Plan.status.in_(("published", "accepted_by_device", "executing")),
            Plan.published_at >= start,
            Plan.published_at <= now,
        )
        .order_by(Plan.version.desc())
        .limit(1)
    )
    lower, upper = total("p10"), total("p90")
    return {
        "kind": "briefing",
        "version": VERSION,
        "day": day.isoformat(),
        "timezone": station.timezone,
        "station_name": station.name,
        "coverage_policy": "complete-daylight; astronomical-night-gaps-only/v1",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "title": titles[kind],
        "body": body + " " + action,
        "expected_kwh": str(expected),
        "display_kwh": display,
        "range_kwh": [str(lower), str(upper)]
        if lower is not None and upper is not None and lower <= expected <= upper
        else None,
        "confidence": "high"
        if all(
            r.confidence == "high" and weather[r.based_on_weather_forecast_id].confidence == "high"
            for r in expected_rows
        )
        else "nominal",
        "forecast_issued_at": issued.isoformat(),
        "forecast_ids": [str(r.id) for r in expected_rows],
        "source_versions": sorted(
            {r.source + ":" + (r.source_version or "unknown") for r in expected_rows}
        ),
        "weather_ids": sorted(str(k) for k in weather),
        "baseline": baseline,
        "classification": kind,
        "action": action,
        "action_start": peak.interval_start.isoformat() if peak else None,
        "action_end": peak.interval_end.isoformat() if peak else None,
        "plan_id": str(plan.id) if plan else None,
        "plan_version": plan.version if plan else None,
        "quality": "estimated",
        "expires_at": min(
            end, issued + MAX_AGE, *(w.issued_at + MAX_AGE for w in weather.values())
        ).isoformat(),
    }


def in_window(pref, station, now):
    tz = station.timezone if pref.briefing_clock == "station" else pref.timezone
    local = now.astimezone(ZoneInfo(tz))
    return (
        pref.briefing_start_hour <= local.hour < pref.briefing_end_hour
        and not notifications.quiet(pref, now)
    )


def window_expiry(pref, station, now):
    # Walk UTC to distinguish repeated times and skip nonexistent local hours.
    candidate = now.replace(second=0, microsecond=0)
    while candidate < now + MAX_AGE and in_window(pref, station, candidate):
        candidate += timedelta(minutes=1)
    return candidate


def materialize(db, now=None):
    now = now or utcnow()
    created = 0
    prefs = db.scalars(
        select(NotificationPreference)
        .where(NotificationPreference.morning_briefing.is_(True))
        .order_by(NotificationPreference.id)
        .with_for_update(skip_locked=True)
    ).all()
    cache = {}
    for pref in prefs:
        if not notifications.member_can_receive(db, pref.user_id, pref.organization_id):
            continue
        for station in db.scalars(
            select(Station)
            .where(Station.organization_id == pref.organization_id, Station.is_active.is_(True))
            .order_by(Station.id)
        ):
            if not in_window(pref, station, now):
                continue
            if station.id not in cache:
                cache[station.id] = forecast_facts(db, station, now)
            facts = cache[station.id]
            if facts is None:
                continue
            key = "morning:" + facts["day"]
            notice_id = db.execute(
                insert(Notification)
                .values(
                    user_id=pref.user_id,
                    station_id=station.id,
                    source_key=key,
                    payload=facts,
                    title=facts["title"],
                    category="briefing",
                    severity="info",
                    link=f"/stations/{station.id}/briefing/{facts['day']}",
                    routed_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_notification_source_user")
                .returning(Notification.id)
            ).scalar_one_or_none()
            if notice_id is None:
                continue
            created += 1
            expiry = min(
                window_expiry(pref, station, now), datetime.fromisoformat(facts["expires_at"])
            )
            for channel in notifications.CHANNELS:
                if pref.matrix.get(f"briefing:info:{channel}", "off") == "off":
                    continue
                # One external digest per user/channel/UTC day, across organizations.
                delivery = notifications._enqueue(
                    db,
                    pref,
                    channel,
                    min(now + timedelta(minutes=10), max(now, expiry - timedelta(minutes=1))),
                    f"morning:{pref.user_id}:{channel}:{now.astimezone(UTC).date()}",
                    [str(notice_id)],
                    "briefing",
                )
                delivery.expires_at = (
                    min(delivery.expires_at, expiry) if delivery.expires_at else expiry
                )
    db.flush()
    return created


def eligible_delivery_notices(db, delivery, now):
    result = []
    for notice, station, pref in db.execute(
        select(Notification, Station, NotificationPreference)
        .join(Station, Station.id == Notification.station_id)
        .join(
            NotificationPreference,
            (NotificationPreference.organization_id == Station.organization_id)
            & (NotificationPreference.user_id == Notification.user_id),
        )
        .where(
            Notification.id.in_(delivery.notification_ids),
            Notification.user_id == delivery.user_id,
            Notification.category == "briefing",
        )
    ):
        if (
            pref.morning_briefing
            and notifications.mode(pref, notice, delivery.channel) != "off"
            and notifications.member_can_receive(db, delivery.user_id, station.organization_id)
            and station.is_active
            and in_window(pref, station, now)
            and now - datetime.fromisoformat(notice.payload["forecast_issued_at"]) <= MAX_AGE
            and now < datetime.fromisoformat(notice.payload["expires_at"])
        ):
            result.append(notice)
    return result
