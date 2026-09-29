"""Bounded mobile composition of the platform's canonical read services."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.config import get_settings
from app.models.device import Device
from app.models.deye_integration import DeyeCloudConnection
from app.models.home_assistant import HomeAssistantConnection, HomeAssistantMapping
from app.models.notification import Notification
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.schemas.mobile import (
    MobileChart,
    MobileChartPoint,
    MobileComparison,
    MobileCost,
    MobileDevice,
    MobileEnergyMetric,
    MobileForecast,
    MobileHAContext,
    MobileHAEntity,
    MobileIntegration,
    MobileIssue,
    MobileMetric,
    MobileOverview,
    MobilePeriod,
    MobileSourceSummary,
    MobileStation,
)
from app.services import dashboard_service as dashboard
from app.services import home_assistant_bridge as bridge_service
from app.services import home_assistant_service as mqtt_service
from app.services.ev_analytics_service import seconds
from app.services.telemetry_diagnostics_service import quality as telemetry_quality

ZERO = Decimal(0)
QUALITY_RANK = {
    "unknown": 0,
    "missing": 0,
    "declared": 1,
    "measured": 2,
    "partial": 3,
    "estimated": 4,
    "stale": 5,
    "simulated": 6,
}


def worst(qualities):
    return max(qualities, key=lambda q: QUALITY_RANK.get(q, 0), default="missing")


def evaluation_time():
    now = datetime.now(UTC)
    return now.replace(second=now.second // 30 * 30, microsecond=0)


def issue(code, resource, message):
    return MobileIssue(code=code, resource=resource, message=message)


def source_summary(db, station, now):
    devices = db.scalars(
        select(Device).where(Device.station_id == station.id).order_by(Device.id).limit(20)
    ).all()
    count = db.scalar(select(func.count(Device.id)).where(Device.station_id == station.id))
    rows = (
        db.scalars(
            select(TelemetryRaw)
            .where(
                TelemetryRaw.station_id == station.id,
                TelemetryRaw.device_id.in_([d.id for d in devices]),
            )
            .distinct(TelemetryRaw.device_id)
            .order_by(
                TelemetryRaw.device_id, TelemetryRaw.measured_at.desc(), TelemetryRaw.id.desc()
            )
        ).all()
        if devices
        else []
    )
    latest = {row.device_id: row for row in rows}
    result = []
    for device in devices:
        row = latest.get(device.id)
        as_of = (
            max(now, row.measured_at)
            if row and row.measured_at <= now + timedelta(seconds=30)
            else now
        )
        q = telemetry_quality(row, as_of) if row else "missing"
        stale = row and (
            (row.quality_flags or {}).get("stale")
            or now - row.measured_at > dashboard.STALE_AFTER
            or row.measured_at > now + timedelta(seconds=30)
        )
        result.append(
            MobileDevice(
                id=device.id,
                name=device.name,
                status=device.status,
                source=row.source if row else "unknown",
                last_heartbeat_at=device.last_heartbeat_at,
                measured_at=row.measured_at if row else None,
                received_at=row.received_at if row else None,
                quality="estimated" if q == "derived" else q,
                freshness="missing" if row is None else "stale" if stale else "fresh",
            )
        )
    return MobileSourceSummary(devices=result, total_devices=count, truncated=count > len(result))


def ha_context(db, station, provider, now):
    entities = []
    consent = False
    if provider == "home_assistant_bridge":
        bridge = bridge_service.get_bridge(db, station.id)
        enabled = get_settings().home_assistant_bridge_enabled and (
            not bridge or bridge_service.owner_active(db, bridge)
        )
        snapshot = bridge_service.snapshot(bridge, enabled=enabled, now=now)
        status, enabled = snapshot["status"], snapshot["enabled"]
        version, last = snapshot["mapping_version"], snapshot["last_seen_at"]
        consent = snapshot["insights_consent"] and enabled
        mapped_units = {m["entity_id"]: m["unit"] for m in bridge.mappings} if bridge else {}
        for item in snapshot["observations"]:
            # Inventory is the allowlist; observations never supply extra entities.
            entities.append(
                MobileHAEntity(
                    entity_id=item["entity_id"],
                    kind=item["kind"],
                    mapped_unit=mapped_units[item["entity_id"]],
                    max_age_seconds=item["max_age_seconds"],
                    available=item["available"],
                    source_quality=item["source_quality"],
                    is_stale=item["is_stale"],
                    is_simulated=item["is_simulated"],
                    metric=MobileMetric(
                        value=item.get("value"),
                        unit=item["unit"],
                        source="home_assistant_bridge",
                        quality=item["quality"],
                        measured_at=item.get("observed_at"),
                        received_at=item.get("received_at"),
                        flags=[
                            q
                            for q, active in (
                                ("stale", item["is_stale"]),
                                ("simulated", item["is_simulated"]),
                            )
                            if active
                        ],
                    ),
                )
            )
    else:
        connection = db.scalar(
            select(HomeAssistantConnection).where(HomeAssistantConnection.station_id == station.id)
        )
        enabled = bool(
            get_settings().home_assistant_mqtt_enabled and connection and connection.enabled
        )
        mappings = (
            db.scalars(
                select(HomeAssistantMapping)
                .where(HomeAssistantMapping.connection_id == connection.id)
                .order_by(HomeAssistantMapping.entity_id)
            ).all()
            if connection and enabled
            else []
        )
        status = connection.status if enabled else "disconnected"
        version, last = (
            (connection.revision, connection.last_connected_at) if connection else (None, None)
        )
        for mapping in mappings:
            item = mqtt_service.observation(mapping, enabled=enabled, now=now)
            entities.append(
                MobileHAEntity(
                    entity_id=mapping.entity_id,
                    kind=mapping.kind,
                    mapped_unit=mapping.unit,
                    max_age_seconds=mapping.max_age_seconds,
                    available=item["available"],
                    source_quality=item["source_quality"],
                    is_stale=item["is_stale"],
                    is_simulated=item["is_simulated"],
                    metric=MobileMetric(
                        value=item["value"],
                        unit=item["unit"],
                        source=item["source"] or "home_assistant_mqtt",
                        quality=item["quality"],
                        measured_at=item["observed_at"],
                        received_at=item["received_at"],
                        flags=[
                            q
                            for q, active in (
                                ("stale", item["is_stale"]),
                                ("simulated", item["is_simulated"]),
                            )
                            if active
                        ],
                    ),
                )
            )
        if enabled and entities and any(e.is_stale for e in entities):
            status = "stale"
    issues = []
    if status == "disconnected":
        issues.append(issue("integration_disconnected", provider, "Integrarea nu este conectata."))
    elif status == "reauth_required":
        issues.append(issue("reauth_required", provider, "Reconecteaza integrarea."))
    elif status in ("stale", "offline", "error", "backoff"):
        issues.append(issue("stale", provider, "Contextul integrarii nu este actualizat."))
    integration = MobileIntegration(
        provider=provider,
        status=status,
        enabled=enabled,
        last_seen_at=last,
        mapping_version=version,
        entity_count=len(entities),
        available_entities=sum(e.available for e in entities),
        issues=issues,
    )
    return MobileHAContext(
        station_id=station.id,
        timezone=station.timezone,
        integration=integration,
        entities=entities,
        insights_consent=consent,
    )


def deye_summary(db, station, now):
    connection = db.scalar(
        select(DeyeCloudConnection).where(DeyeCloudConnection.station_id == station.id)
    )
    status = connection.status if connection else "disconnected"
    issues = []
    if status == "disconnected":
        issues.append(issue("provider_disconnected", "deye_cloud", "Deye Cloud nu este conectat."))
    elif status == "error":
        issues.append(issue("reauth_required", "deye_cloud", "Reconecteaza contul Deye Cloud."))
    elif status == "connected" and (
        connection.last_sync_at is None or now - connection.last_sync_at > dashboard.STALE_AFTER
    ):
        issues.append(issue("stale", "deye_cloud", "Datele Deye Cloud nu sunt actualizate."))
    return MobileIntegration(
        provider="deye_cloud",
        status=status,
        enabled=status == "connected",
        last_seen_at=connection.last_sync_at if connection else None,
        issues=issues,
    )


def forecast_summary(db, station, now):
    end = now + timedelta(hours=24)
    points = dashboard.get_forecast_vs_actual(db, station, "pv", now, end, now=now, exact=True)
    energy, covered = ZERO, ZERO
    qualities, sources, confidences, issued = set(), set(), set(), []
    for point in points:
        left = max(datetime.fromisoformat(point["t"]), now)
        right = min(point["interval_end"], end)
        if right <= left:
            continue
        duration = seconds(right - left)
        energy += point["forecast_kw"] * duration / 3600
        covered += duration
        sources.add(point["forecast_source"])
        confidences.add(point["forecast_confidence"])
        at = datetime.fromisoformat(point["forecast_issued_at"])
        issued.append(at)
        qualities.add("estimated")
        if point["is_synthetic"] or (point.get("weather") or {}).get("is_synthetic"):
            qualities.add("simulated")
        if now - at > timedelta(hours=6):
            qualities.add("stale")
    return MobileForecast(
        start=now,
        end=end,
        issued_at=max(issued, default=None),
        confidence=next(
            (level for level in ("low", "medium", "high") if level in confidences), None
        ),
        pv_energy=MobileMetric(
            value=energy if covered else None,
            unit="kWh",
            source=",".join(sorted(sources)) or None,
            quality=worst(qualities),
            coverage=min(covered / 86400, Decimal(1)),
            flags=sorted(qualities - {"estimated"}),
            updated_at=max(issued, default=None),
        ),
    )


def cost_summary(db, station, start, end):
    result = dashboard.get_estimated_savings(db, station, start, end, exact=True)
    available = result["available"]
    qualities = result.get("input_qualities", [])
    if available and result.get("coverage_ratio") is not None and result["coverage_ratio"] < 1:
        qualities = [*qualities, "partial"]
    q = worst(["estimated", *qualities]) if available else "missing"

    def metric(key):
        return MobileMetric(
            value=result.get(key),
            unit="RON",
            source="historical_tariffs_and_telemetry",
            quality=q,
            updated_at=result.get("updated_at"),
            coverage=result.get("coverage_ratio"),
            flags=qualities,
        )

    return MobileCost(
        net_cost=metric("actual_net_cost_lei"),
        system_benefit=metric("whole_system_benefit_lei"),
        reason=result.get("reason"),
    )


def overview(db, station, user, role, *, now=None):
    now = now or evaluation_time()
    raw_live = {
        m["metric"]: m for m in dashboard.get_live_metrics(db, station, now=now, exact=True)
    }
    source = raw_live["telemetry_source"]["value"]
    live = {}
    for key in (
        "pv_power_kw",
        "load_power_kw",
        "battery_power_kw",
        "grid_power_kw",
        "battery_soc_percent",
        "ev_connected",
        "ev_power_kw",
    ):
        item = raw_live[key]
        live[key] = MobileMetric(
            **{k: item[k] for k in ("value", "unit", "measured_at", "received_at")},
            source=source,
            quality=item["quality"] if item["value"] is not None else "missing",
            flags=item["flags"],
        )
    energy = {}
    for name, period in dashboard.get_energy_period_kpis(db, station, now=now, exact=True).items():
        metrics = {}
        for key, item in period["metrics"].items():
            q = period["source_quality"] if item["value"] is not None else "missing"
            comparison = (
                MobileComparison(
                    **item["comparison"], quality=worst([q, *period["comparison_qualities"]])
                )
                if item["comparison"]
                else None
            )
            metrics[key] = MobileEnergyMetric(
                value=item["value"],
                unit="kWh",
                source="telemetry_aggregate",
                quality=q,
                updated_at=period["updated_at"],
                coverage=item["coverage"],
                comparison=comparison,
            )
        energy[name] = MobilePeriod(
            start=period["period_start"],
            end=period["period_end"],
            comparison_label=period["comparison_label"],
            metrics=metrics,
        )
    integrations = [
        deye_summary(db, station, now),
        *[
            ha_context(db, station, p, now).integration
            for p in ("home_assistant_bridge", "home_assistant_mqtt")
        ],
    ]
    notices = db.scalar(
        select(func.count(Notification.id)).where(
            Notification.station_id == station.id,
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
        )
    )
    issues = [i for integration in integrations for i in integration.issues]
    if (
        raw_live["data_quality"]["value"] in ("stale", "missing")
        or "stale" in raw_live["data_quality"]["flags"]
    ):
        issues.append(issue("stale", "telemetry", "Telemetria este veche sau indisponibila."))
    return MobileOverview(
        evaluated_at=now,
        station=MobileStation(
            id=station.id,
            organization_id=station.organization_id,
            name=station.name,
            timezone=station.timezone,
            role=role,
        ),
        live=live,
        energy=energy,
        forecast=forecast_summary(db, station, now),
        costs={key: cost_summary(db, station, period.start, now) for key, period in energy.items()},
        unread_notifications=notices,
        sources=source_summary(db, station, now),
        integrations=integrations,
        issues=issues,
    )


def chart(db, station, range_key, resolution, *, end=None):
    end = end or evaluation_time()
    days = {"24h": 1, "7d": 7, "30d": 30, "1y": 365}[range_key]
    resolution = resolution or {"24h": "15m", "7d": "1h", "30d": "1h", "1y": "1d"}[range_key]
    if days * {"15m": 96, "1h": 24, "1d": 1}[resolution] > 768:
        raise ValueError("Alege o rezolutie mai rara pentru acest interval.")
    start = end - timedelta(days=days)
    period_type = {"15m": "interval_15m", "1h": "hour", "1d": "day"}[resolution]
    # Energy/SOC already use the platform's canonical integrator and station calendar.
    rows = db.scalars(
        select(TelemetryAggregate)
        .where(
            TelemetryAggregate.station_id == station.id,
            TelemetryAggregate.period_type == period_type,
            TelemetryAggregate.period_start >= start,
            TelemetryAggregate.period_start < end,
        )
        .order_by(TelemetryAggregate.period_start)
        .limit(769)
    ).all()
    fields = {
        "pv": "pv_energy_kwh",
        "load": "load_energy_kwh",
        "grid_import": "grid_import_energy_kwh",
        "grid_export": "grid_export_energy_kwh",
        "battery_charge": "battery_charge_energy_kwh",
        "battery_discharge": "battery_discharge_energy_kwh",
        "soc": "avg_battery_soc_percent",
    }
    points = [
        MobileChartPoint(
            start=r.period_start,
            end=r.period_end,
            values={key: getattr(r, field) for key, field in fields.items()},
            coverage={
                key: (r.coverage or {}).get(
                    "grid"
                    if key.startswith("grid")
                    else "battery"
                    if key.startswith("battery")
                    else key,
                    0,
                )
                for key in fields
            },
            quality=r.data_quality,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
    return MobileChart(
        station_id=station.id,
        timezone=station.timezone,
        start=start,
        end=end,
        resolution=resolution,
        units={key: "%" if key == "soc" else "kWh" for key in fields},
        aggregation={key: "mean" if key == "soc" else "sum" for key in fields},
        points=points,
    )
