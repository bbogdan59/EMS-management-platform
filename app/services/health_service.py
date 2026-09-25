from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.audit import record_audit
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.command import Command
from app.models.device import Device
from app.models.firmware import FirmwareDeployment
from app.models.forecast import PvForecast, WeatherForecast
from app.models.health import AlertEvent, HealthEvaluation, HealthState
from app.models.market import MarketPriceInterval
from app.models.optimization import OptimizationRun
from app.models.station import Station
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services.device_service import get_active_station_config
from app.services.telemetry_diagnostics_service import quality

ACTIVE = ("open", "detected", "active", "acknowledged", "resolving")
TERMINAL = ("resolved", "suppressed", "expired", "false_positive")


@dataclass(frozen=True)
class Rule:
    code: str
    title: str
    required: str
    threshold: str
    action: str
    severity: str = "warning"
    category: str = "warning"
    version: int = 1
    window_minutes: int = 5
    confidence: str = "high"


RULES = {
    r.code: r
    for r in [
        Rule(
            "device_offline",
            "Dispozitiv deconectat",
            "heartbeat",
            ">15 minute",
            "Verifica alimentarea si conexiunea la internet.",
        ),
        Rule(
            "telemetry_stale",
            "Masuratori intarziate",
            "measured_at",
            ">10 minute",
            "Verifica citirea invertorului si coada de incarcare.",
        ),
        Rule(
            "data_quality",
            "Date incomplete sau neobservate",
            "PV, load, grid, battery SOC",
            "metrica lipsa ori simulated/stale/derived",
            "Verifica senzorii si provenienta datelor.",
        ),
        Rule(
            "inverter_fault",
            "Invertorul raporteaza o alarma",
            "status validat si proaspat",
            "fault sau severity error/critical",
            "Consulta codul de alarma in manualul modelului; contacteaza instalatorul.",
            "critical",
            "incident",
        ),
        Rule(
            "pv_under_forecast",
            "Productie sub prognoza meteo",
            "agregat 15m >=80%, prognoza meteo reala",
            "<50% din prognoza; revenire >=65%",
            "Verifica umbrirea si prognoza; comparatia este o estimare.",
            confidence="medium",
            window_minutes=15,
        ),
        Rule(
            "night_load",
            "Consum nocturn neobisnuit",
            "ora locala 00-05, minimum 3 nopti comparabile",
            ">150% din mediana; revenire <=125%",
            "Verifica aparatele ramase pornite.",
            confidence="medium",
            window_minutes=60,
        ),
        Rule(
            "battery_temperature",
            "Temperatura bateriei ridicata",
            "temperatura masurata",
            ">50 C; revenire <=45 C",
            "Verifica ventilatia si limitele producatorului.",
            "critical",
            "incident",
        ),
        Rule(
            "battery_soc",
            "SOC in afara rezervei configurate",
            "SOC masurat si preferinte",
            "sub minim sau peste maxim; hysteresis 2 puncte",
            "Verifica rezerva si planul; nu modifica protectiile bateriei.",
        ),
        Rule(
            "battery_soh",
            "Starea bateriei necesita verificare",
            "SOH masurat",
            "<70%; revenire >=75%",
            "Compara SOH cu diagnosticul producatorului.",
        ),
        Rule(
            "grid_limit",
            "Limita de retea depasita",
            "putere grid masurata, limite configurate",
            "peste limita; revenire sub 95%",
            "Redu consumul flexibil si verifica limitele instalatiei.",
            "critical",
            "incident",
        ),
        Rule(
            "command_failed",
            "Comanda fara confirmare de executie",
            "comanda",
            "rejected/failed/expired sau termen depasit",
            "Verifica dispozitivul si istoricul comenzii; ACK nu confirma aplicarea.",
        ),
        Rule(
            "optimization_failed",
            "Planul nu a putut fi calculat",
            "optimization run",
            "failed sau fallback",
            "Verifica datele si constrangerile planului.",
        ),
        Rule(
            "weather_stale",
            "Prognoza meteo indisponibila",
            "meteo nesintetic",
            "lipsa sau mai vechi de 6 ore",
            "Asteapta actualizarea sursei meteo.",
        ),
        Rule(
            "market_stale",
            "Preturi de piata indisponibile",
            "pret OPCOM real pentru interval",
            "interval curent lipsa",
            "Verifica importul OPCOM; nu folosi preturi inventate.",
        ),
        Rule(
            "ota_failed",
            "Actualizarea agentului necesita verificare",
            "firmware deployment",
            "failed/timed_out/rolled_back",
            "Verifica versiunea raportata si istoricul actualizarii.",
        ),
    ]
}


@dataclass
class Finding:
    rule: str
    bad: bool | None
    evidence: dict
    subject: str = "station"
    recovered: bool = True


def _value(value):
    return str(value) if isinstance(value, Decimal) else value


def _numeric(code, value, bad, recovered, **evidence):
    return Finding(
        code,
        None if value is None else bool(bad),
        {"value": _value(value), **evidence},
        recovered=bool(recovered),
    )


def findings(db, station, at, *, historical=False):
    config, pref = get_active_station_config(db, station.id)
    if historical:
        from app.models.preference import PreferenceVersion
        from app.models.station import StationConfigVersion

        config = db.scalar(
            select(StationConfigVersion)
            .where(
                StationConfigVersion.station_id == station.id, StationConfigVersion.created_at <= at
            )
            .order_by(StationConfigVersion.version.desc())
            .limit(1)
        )
        pref = db.scalar(
            select(PreferenceVersion)
            .where(PreferenceVersion.station_id == station.id, PreferenceVersion.created_at <= at)
            .order_by(PreferenceVersion.version.desc())
            .limit(1)
        )
    devices = db.scalars(select(Device).where(Device.station_id == station.id)).all()
    for device in devices:
        if not device.capabilities.get("deye_cloud"):
            age = (
                max(0, (at - device.last_heartbeat_at).total_seconds())
                if device.last_heartbeat_at and not historical
                else None
            )
            yield Finding(
                "device_offline",
                None if historical else device.status == "active" and (age is None or age > 900),
                {"age_seconds": age, "threshold_seconds": 900, "device_id": str(device.id)},
                str(device.id),
            )
    row = db.scalar(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station.id, TelemetryRaw.measured_at <= at)
        .order_by(TelemetryRaw.measured_at.desc(), TelemetryRaw.id)
        .limit(1)
    )
    q = quality(row, at) if row else "missing"
    base = {
        "quality": q,
        "measured_at": row.measured_at.isoformat() if row else None,
        "telemetry_id": str(row.id) if row else None,
        "source": row.source if row else None,
    }
    yield Finding(
        "telemetry_stale", row is None or at - row.measured_at > timedelta(minutes=10), base
    )
    required = ["pv_power_w", "load_power_w", "grid_power_w"]
    if config and config.battery_reference_capacity_kwh is not None:
        required.append("battery_soc_percent")
    missing = [key for key in required if row is None or getattr(row, key) is None]
    yield Finding("data_quality", bool(missing) or q != "measured", {**base, "missing": missing})
    trusted = row is not None and q == "measured"
    ext = row.diagnostics if trusted else {}
    battery = ext.get("battery", {})
    status = ext.get("status", {})
    if battery.get("quality", "measured") != "measured":
        battery = {}
    valid_status = status and status.get("quality") in ("reported", "measured")
    yield Finding(
        "inverter_fault",
        bool(
            status.get("inverter_state") == "fault"
            or status.get("battery_state") == "fault"
            or any(f["severity"] in ("error", "critical") for f in status.get("faults", []))
        )
        if valid_status
        else None,
        {**base, "fault_codes": [f["code"] for f in status.get("faults", [])]},
    )
    temp = Decimal(battery["temperature_c"]) if battery.get("temperature_c") is not None else None
    soh = Decimal(battery["soh_percent"]) if battery.get("soh_percent") is not None else None
    yield _numeric(
        "battery_temperature",
        temp,
        temp is not None and temp > 50,
        temp is not None and temp <= 45,
        **base,
    )
    yield _numeric(
        "battery_soh", soh, soh is not None and soh < 70, soh is not None and soh >= 75, **base
    )
    soc = row.battery_soc_percent if trusted and pref else None
    low, high = (
        (pref.min_reserve_soc_percent, pref.max_normal_soc_percent) if pref else (None, None)
    )
    yield _numeric(
        "battery_soc",
        soc,
        soc is not None and (soc < low or soc > high),
        soc is not None and min(low + 2, high) <= soc <= max(high - 2, low),
        minimum=_value(low),
        maximum=_value(high),
        **base,
    )
    power = row.grid_power_w / 1000 if trusted and row.grid_power_w is not None else None
    limit = (
        (config.grid_import_limit_kw if power >= 0 else config.grid_export_limit_kw)
        if config and power is not None
        else None
    )
    yield _numeric(
        "grid_limit",
        power if limit is not None else None,
        power is not None and limit is not None and abs(power) > limit,
        power is not None and limit is not None and abs(power) <= limit * Decimal(".95"),
        limit_kw=_value(limit),
        **base,
    )
    end = at.replace(minute=at.minute // 15 * 15, second=0, microsecond=0)
    agg = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == "interval_15m",
            TelemetryAggregate.period_start == end - timedelta(minutes=15),
        )
    )
    forecast = db.scalar(
        select(PvForecast)
        .where(
            PvForecast.station_id == station.id,
            PvForecast.interval_start == end - timedelta(minutes=15),
            PvForecast.scenario == "expected",
            PvForecast.issued_at <= end,
            PvForecast.issued_at >= end - timedelta(hours=6),
            PvForecast.is_synthetic.is_(False),
        )
        .order_by(PvForecast.issued_at.desc())
        .limit(1)
    )
    weather = (
        db.get(WeatherForecast, forecast.based_on_weather_forecast_id)
        if forecast and forecast.based_on_weather_forecast_id
        else None
    )
    expected = (
        forecast.predicted_power_kw / 4
        if forecast and weather and not weather.is_synthetic
        else None
    )
    actual = (
        agg.pv_energy_kwh
        if agg
        and agg.data_quality == "measured"
        and Decimal(str(agg.coverage.get("pv", 0))) >= Decimal(".8")
        else None
    )
    ratio = (
        actual / expected
        if actual is not None and expected is not None and expected >= Decimal(".25")
        else None
    )
    yield _numeric(
        "pv_under_forecast",
        ratio,
        ratio is not None and ratio < Decimal(".5"),
        ratio is not None and ratio >= Decimal(".65"),
        actual_kwh=_value(actual),
        forecast_kwh=_value(expected),
        formula="actual_kwh / weather_adjusted_forecast_kwh",
        aggregate_id=str(agg.id) if agg else None,
        forecast_id=str(forecast.id) if forecast else None,
    )
    local = at.astimezone(ZoneInfo(station.timezone))
    baseline, current = [], None
    if local.hour < 6:
        hours = db.scalars(
            select(TelemetryAggregate)
            .where(
                TelemetryAggregate.station_id == station.id,
                TelemetryAggregate.period_type == "hour",
                TelemetryAggregate.period_start >= at - timedelta(days=8),
                TelemetryAggregate.period_end <= at,
                TelemetryAggregate.data_quality == "measured",
            )
            .order_by(TelemetryAggregate.period_start.desc())
        ).all()
        for hour in hours:
            if hour.load_energy_kwh is None or Decimal(str(hour.coverage.get("load", 0))) < Decimal(
                ".8"
            ):
                continue
            if current is None and at - hour.period_end < timedelta(hours=1):
                current = hour
            elif (
                current
                and hour.period_start.astimezone(ZoneInfo(station.timezone)).hour
                == current.period_start.astimezone(ZoneInfo(station.timezone)).hour
            ):
                baseline.append(hour.load_energy_kwh)
    if current and current.period_start.astimezone(ZoneInfo(station.timezone)).hour >= 6:
        current = None
    median = sorted(baseline)[len(baseline) // 2] if len(baseline) >= 3 else None
    ratio = (
        current.load_energy_kwh / median if current and median is not None and median > 0 else None
    )
    yield _numeric(
        "night_load",
        ratio,
        ratio is not None and ratio > Decimal("1.5"),
        ratio is not None and ratio <= Decimal("1.25"),
        baseline_kwh=_value(median),
        baseline_nights=len(baseline),
        timezone=station.timezone,
    )
    latest_weather = db.scalar(
        select(WeatherForecast)
        .where(
            WeatherForecast.station_id == station.id,
            WeatherForecast.issued_at <= at,
            WeatherForecast.is_synthetic.is_(False),
            WeatherForecast.interval_start <= at,
            WeatherForecast.interval_end > at,
        )
        .order_by(WeatherForecast.issued_at.desc())
        .limit(1)
    )
    yield Finding(
        "weather_stale",
        latest_weather is None or at - latest_weather.issued_at > timedelta(hours=6),
        {"issued_at": latest_weather.issued_at.isoformat() if latest_weather else None},
    )
    market = db.scalar(
        select(MarketPriceInterval)
        .where(
            MarketPriceInterval.interval_start <= at,
            MarketPriceInterval.interval_end > at,
            MarketPriceInterval.is_current.is_(True),
        )
        .limit(1)
    )
    from app.models.market import ImportRun

    run = db.get(ImportRun, market.import_run_id) if market else None
    yield Finding(
        "market_stale",
        market is None or run is None or run.is_synthetic_fixture,
        {
            "interval_id": str(market.id) if market else None,
            "synthetic": run.is_synthetic_fixture if run else None,
        },
    )
    commands = db.scalars(
        select(Command).where(
            Command.station_id == station.id,
            Command.valid_from <= at,
            Command.valid_from >= at - timedelta(days=1),
        )
    ).all()
    for command in commands:
        if historical:
            from app.models.command import CommandEvent

            event = db.scalar(
                select(CommandEvent)
                .where(CommandEvent.command_id == command.id, CommandEvent.created_at <= at)
                .order_by(CommandEvent.created_at.desc())
                .limit(1)
            )
            command_status = event.event_type if event else None
        else:
            command_status = command.status
        yield Finding(
            "command_failed",
            None
            if command_status is None
            else command_status in ("rejected", "failed", "expired")
            or (command.expires_at < at and command_status not in ("executed", "superseded")),
            {"status": command_status, "expires_at": command.expires_at.isoformat()},
            str(command.id),
        )
    run = db.scalar(
        select(OptimizationRun)
        .where(OptimizationRun.station_id == station.id, OptimizationRun.created_at <= at)
        .order_by(OptimizationRun.created_at.desc())
        .limit(1)
    )
    yield Finding(
        "optimization_failed",
        run.status in ("failed", "infeasible", "solver_timeout", "fallback") or run.is_fallback
        if run and (not historical or (run.finished_at and run.finished_at <= at))
        else None,
        {"run_id": str(run.id) if run else None, "status": run.status if run else None},
    )
    deployment = db.scalar(
        select(FirmwareDeployment)
        .join(Device, Device.id == FirmwareDeployment.device_id)
        .where(Device.station_id == station.id, FirmwareDeployment.requested_at <= at)
        .order_by(FirmwareDeployment.requested_at.desc())
        .limit(1)
    )
    deployment_status = deployment.status if deployment else None
    if historical and deployment:
        from app.models.firmware import FirmwareDeploymentEvent

        event = db.scalar(
            select(FirmwareDeploymentEvent)
            .where(
                FirmwareDeploymentEvent.deployment_id == deployment.id,
                FirmwareDeploymentEvent.created_at <= at,
            )
            .order_by(FirmwareDeploymentEvent.created_at.desc())
            .limit(1)
        )
        deployment_status = event.event_type if event else None
    yield Finding(
        "ota_failed",
        deployment_status in ("failed", "timed_out", "rolled_back") if deployment_status else None,
        {
            "deployment_id": str(deployment.id) if deployment else None,
            "status": deployment_status,
        },
    )


def transition(db, station, alert, status, at, reason, actor=None):
    alert.status = status
    if status == "acknowledged":
        alert.acknowledged_at, alert.acknowledged_by_user_id = at, actor.id if actor else None
    if status in TERMINAL:
        alert.resolved_at = at
    event = AlertEvent(
        alert_id=alert.id,
        status=status,
        occurred_at=at,
        actor_user_id=actor.id if actor else None,
        reason=reason,
    )
    db.add(event)
    record_audit(
        db,
        action=f"health.{status}",
        resource_type="alert",
        resource_id=str(alert.id),
        station_id=station.id,
        organization_id=station.organization_id,
        actor_user_id=actor.id if actor else None,
        metadata={"reason": reason},
    )
    db.flush()
    return event


def evaluate_station(db, station: Station, at: datetime | None = None, *, historical=False):
    at = at or utcnow()
    if at.tzinfo is None:
        raise ValueError("Evaluarea necesita un instant cu fus orar.")
    if historical and at >= utcnow() - timedelta(minutes=5):
        raise ValueError("Reevaluarea istorica necesita o fereastra deja incheiata.")
    at = at.astimezone(UTC).replace(second=0, microsecond=0)
    at = at.replace(minute=at.minute // 5 * 5)
    # The station row serializes creation, feedback and retries across workers.
    db.execute(
        select(Station.id).where(Station.id == station.id).with_for_update(key_share=True)
    ).scalar_one()
    created = 0
    for finding in findings(db, station, at, historical=historical):
        rule = RULES[finding.rule]
        evidence = {
            **finding.evidence,
            "rule": asdict(rule),
            "window_end": at.isoformat(),
            "historical": historical,
        }
        verdict = (
            "unknown"
            if finding.bad is None
            else ("bad" if finding.bad else "healthy" if finding.recovered else "recovering")
        )
        inserted = db.execute(
            insert(HealthEvaluation)
            .values(
                station_id=station.id,
                rule=rule.code,
                subject=finding.subject,
                version=rule.version,
                window_end=at,
                verdict=verdict,
                evidence=evidence,
            )
            .on_conflict_do_nothing(constraint="uq_health_evaluation")
            .returning(HealthEvaluation.id)
        ).scalar_one_or_none()
        if inserted is None or historical:
            continue
        state = db.scalar(
            select(HealthState).where(
                HealthState.station_id == station.id,
                HealthState.rule == rule.code,
                HealthState.subject == finding.subject,
            )
        )
        if state is None:
            state = HealthState(
                station_id=station.id, rule=rule.code, subject=finding.subject, healthy_windows=0
            )
            db.add(state)
            if rule.code == "device_offline":
                legacy = db.scalars(
                    select(Alert).where(
                        Alert.station_id == station.id,
                        Alert.category == rule.code,
                        Alert.status == "open",
                    )
                ).all()
                match = next(
                    (a for a in legacy if a.context.get("device_id") == finding.subject), None
                )
                if match:
                    state.alert_id = match.id
                    transition(db, station, match, "active", at, "legacy_incident_adopted")
        if state.evaluated_at and at <= state.evaluated_at:
            continue
        previous_at = state.evaluated_at
        state.evaluated_at = at
        alert = db.get(Alert, state.alert_id) if state.alert_id else None
        if finding.bad:
            state.healthy_windows = 0
            if alert and alert.status in ACTIVE:
                alert.context = evidence
                if alert.status == "resolving":
                    transition(db, station, alert, "active", at, "condition_returned")
            elif state.cooldown_until is None or at >= state.cooldown_until:
                alert = Alert(
                    station_id=station.id,
                    category=rule.code,
                    severity=rule.severity,
                    status="detected",
                    created_at=at,
                    title=rule.title,
                    description=rule.action,
                    context=evidence,
                )
                db.add(alert)
                db.flush()
                state.alert_id = alert.id
                transition(db, station, alert, "detected", at, "threshold_crossed")
                transition(db, station, alert, "active", at, "rule_confirmed")
                created += 1
        elif alert and alert.status in ACTIVE:
            if finding.bad is None:
                state.healthy_windows = 0
                last_evidence_at = db.scalar(
                    select(HealthEvaluation.window_end)
                    .where(
                        HealthEvaluation.station_id == station.id,
                        HealthEvaluation.rule == rule.code,
                        HealthEvaluation.subject == finding.subject,
                        HealthEvaluation.verdict != "unknown",
                        HealthEvaluation.window_end <= at,
                    )
                    .order_by(HealthEvaluation.window_end.desc())
                    .limit(1)
                )
                if at - (last_evidence_at or alert.created_at) > timedelta(days=1):
                    transition(db, station, alert, "expired", at, "evidence_unavailable")
            elif finding.recovered:
                state.healthy_windows = (
                    state.healthy_windows + 1 if previous_at == at - timedelta(minutes=5) else 1
                )
                if state.healthy_windows >= 2:
                    transition(db, station, alert, "resolved", at, "two_healthy_windows")
                    state.cooldown_until = at + timedelta(minutes=30)
                elif alert.status != "resolving":
                    transition(db, station, alert, "resolving", at, "recovery_observed")
            else:
                state.healthy_windows = 0
    db.flush()
    return created


def feedback(db, station, alert_id, action, reason, actor):
    if (
        action not in ("acknowledged", "resolved", "suppressed", "false_positive")
        or not reason.strip()
        or len(reason) > 500
    ):
        raise ValueError("Actiune sau motiv invalid.")
    db.execute(
        select(Station.id).where(Station.id == station.id).with_for_update(key_share=True)
    ).scalar_one()
    alert = db.scalar(
        select(Alert).where(Alert.id == alert_id, Alert.station_id == station.id).with_for_update()
    )
    if alert is None:
        raise LookupError("Alerta inexistenta.")
    if alert.status == action:
        return alert
    if alert.status not in ACTIVE:
        raise ValueError("Alerta este deja inchisa.")
    at = utcnow()
    transition(db, station, alert, action, at, reason.strip(), actor)
    state = db.scalar(select(HealthState).where(HealthState.alert_id == alert.id))
    if state and action in TERMINAL:
        state.cooldown_until = at + (
            timedelta(hours=24)
            if action in ("suppressed", "false_positive")
            else timedelta(minutes=30)
        )
    return alert
