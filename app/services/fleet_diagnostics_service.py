from __future__ import annotations

import contextlib
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.api.deps import StationAccess
from app.core.audit import record_audit
from app.core.security import utcnow
from app.models.alert import Alert
from app.models.command import Command, CommandEvent
from app.models.device import Device, DeviceLogEntry
from app.models.firmware import FirmwareDeployment, FirmwareDeploymentEvent
from app.models.health import AlertEvent, DiagnosticGrant, HealthEvaluation
from app.models.organization import Membership, Organization
from app.models.station import Station, StationConfigVersion
from app.models.telemetry import TelemetryRaw
from app.models.user import User
from app.services.health_service import RULES
from app.services.telemetry_diagnostics_service import quality


def diagnostic_access(db, user, station_id, at=None):
    at = at or utcnow()
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "Statia nu exista.")
    try:
        return StationAccess("viewer")(station_id=station_id, db=db, user=user)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
    org = db.get(Organization, station.organization_id)
    grant = db.scalar(
        select(DiagnosticGrant).where(
            DiagnosticGrant.station_id == station_id,
            DiagnosticGrant.user_id == user.id,
            DiagnosticGrant.revoked_at.is_(None),
            DiagnosticGrant.expires_at > at,
        )
    )
    if grant is None or not user.is_active or org.status != "active":
        raise HTTPException(403, "Acces de diagnostic indisponibil sau expirat.")
    return station, "diagnostic"


def grant_access(db, actor, station, user_id, expires_at, *, revoke=False):
    StationAccess("organization_admin")(station_id=station.id, db=db, user=actor)
    if not revoke and (
        expires_at.tzinfo is None or not utcnow() < expires_at <= utcnow() + timedelta(days=30)
    ):
        raise ValueError("Accesul trebuie sa expire in maximum 30 zile.")
    target = db.get(User, user_id)
    if target is None or not target.is_active:
        raise ValueError("Utilizator indisponibil.")
    db.execute(
        select(Station.id).where(Station.id == station.id).with_for_update(key_share=True)
    ).scalar_one()
    grant = db.scalar(
        select(DiagnosticGrant).where(
            DiagnosticGrant.station_id == station.id, DiagnosticGrant.user_id == user_id
        )
    )
    if grant is None:
        grant = DiagnosticGrant(
            station_id=station.id, user_id=user_id, granted_by=actor.id, expires_at=expires_at
        )
        db.add(grant)
    grant.expires_at, grant.granted_by = expires_at, actor.id
    grant.revoked_at = utcnow() if revoke else None
    record_audit(
        db,
        action="diagnostic.revoke" if revoke else "diagnostic.grant",
        resource_type="station",
        resource_id=str(station.id),
        actor_user_id=actor.id,
        organization_id=station.organization_id,
        station_id=station.id,
        metadata={"user_id": str(user_id), "expires_at": expires_at.isoformat()},
    )
    return grant


def scoped_stations(db, user, at=None):
    at = at or utcnow()
    query = select(Station).join(Organization, Organization.id == Station.organization_id)
    if not user.is_platform_admin:
        members = select(Membership.organization_id).where(
            Membership.user_id == user.id, Membership.is_active.is_(True)
        )
        grants = select(DiagnosticGrant.station_id).where(
            DiagnosticGrant.user_id == user.id,
            DiagnosticGrant.revoked_at.is_(None),
            DiagnosticGrant.expires_at > at,
        )
        query = query.where(
            Organization.status != "archived",
            or_(
                Station.organization_id.in_(members),
                (Station.id.in_(grants) & (Organization.status == "active")),
            ),
        )
    return query.order_by(Station.name, Station.id)


def health_summary(db, station, at=None):
    at = at or utcnow()
    rows = db.scalars(
        select(HealthEvaluation)
        .where(
            HealthEvaluation.station_id == station.id,
            HealthEvaluation.window_end <= at,
            HealthEvaluation.window_end >= at - timedelta(minutes=15),
        )
        .order_by(HealthEvaluation.window_end.desc())
    ).all()
    latest = {}
    for row in rows:
        if not row.evidence.get("historical"):
            latest.setdefault((row.rule, row.subject), row)
    components = [
        {
            "rule": r.rule,
            "subject": r.subject,
            "verdict": r.verdict,
            "threshold": RULES[r.rule].threshold,
            "severity": RULES[r.rule].severity,
            "evaluated_at": r.window_end.isoformat(),
        }
        for r in latest.values()
        if r.rule in RULES
    ]
    known = [c for c in components if c["verdict"] != "unknown"]
    bad = [c for c in known if c["verdict"] in ("bad", "recovering")]
    score = round(100 * (len(known) - len(bad)) / len(known)) if known else None
    severity = (
        "critical"
        if any(c["severity"] == "critical" for c in bad)
        else "warning"
        if bad
        else "info"
        if known
        else "unknown"
    )
    devices = db.scalars(
        select(Device).where(Device.station_id == station.id, Device.status == "active")
    ).all()
    sample = db.scalar(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station.id, TelemetryRaw.measured_at <= at)
        .order_by(TelemetryRaw.measured_at.desc())
        .limit(1)
    )
    update = db.scalar(
        select(FirmwareDeployment)
        .join(Device, Device.id == FirmwareDeployment.device_id)
        .where(Device.station_id == station.id)
        .order_by(FirmwareDeployment.requested_at.desc())
        .limit(1)
    )
    return {
        "station_id": str(station.id),
        "name": station.name,
        "score": score,
        "severity": severity,
        "known_components": len(known),
        "unknown_components": len(components) - len(known),
        "components": components,
        "quality": quality(sample, at) if sample else "missing",
        "online": bool(sample and at - sample.measured_at <= timedelta(minutes=10)),
        "firmware": sorted({d.firmware_version for d in devices if d.firmware_version}),
        "fault": any(c["rule"] == "inverter_fault" for c in bad),
        "update_state": update.status if update else "none",
    }


def _inventory(value):
    return value if value and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}", value) else None


def sanitized_evidence(evidence):
    result = {}
    numeric = (
        "value",
        "limit_kw",
        "minimum",
        "maximum",
        "actual_kwh",
        "forecast_kwh",
        "age_seconds",
        "threshold_seconds",
        "baseline_kwh",
        "baseline_nights",
    )
    for key in numeric:
        value = evidence.get(key)
        try:
            number = Decimal(str(value)) if value is not None else None
        except InvalidOperation:
            continue
        if number is None or number.is_finite():
            result[key] = str(number) if number is not None else None
    for key in ("telemetry_id", "aggregate_id", "forecast_id"):
        with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
            result[key] = str(uuid.UUID(evidence[key]))
    for key in ("measured_at", "window_end"):
        try:
            parsed = datetime.fromisoformat(evidence[key])
            if parsed.tzinfo is not None:
                result[key] = parsed.isoformat()
        except (ValueError, KeyError, TypeError):
            pass
    for key, allowed in (
        ("quality", ("measured", "derived", "simulated", "stale", "missing")),
        ("source", ("device_rs485", "deye_cloud")),
    ):
        if evidence.get(key) in allowed:
            result[key] = evidence[key]
    return result


def latest_diagnostics(db, station, at=None):
    at = at or utcnow()
    row = db.scalar(
        select(TelemetryRaw)
        .where(TelemetryRaw.station_id == station.id, TelemetryRaw.measured_at <= at)
        .order_by(TelemetryRaw.measured_at.desc())
        .limit(1)
    )
    if row is None:
        return {"quality": "missing", "metrics": {}, "queued_hours": 0}
    from sqlalchemy import func

    from app.models.telemetry import TelemetryBackfill

    diagnostics = {
        k: v
        for k, v in row.diagnostics.items()
        if k in ("mppt", "phases", "battery", "inverter", "counters")
    }
    status = row.diagnostics.get("status", {})
    diagnostics["status"] = {
        k: v for k, v in status.items() if k in ("quality", "inverter_state", "battery_state")
    }
    diagnostics["fault_codes"] = [
        f["code"] if re.fullmatch(r"[FW]?[0-9]{1,6}", f.get("code", "")) else "unrecognized"
        for f in status.get("faults", [])
    ]
    # Reset identifiers are arbitrary provider text and are not exported to installers.
    diagnostics["counters"] = [
        {k: v for k, v in c.items() if k != "reset_id"} for c in diagnostics.get("counters", [])
    ]
    return {
        "quality": quality(row, at),
        "measured_at": row.measured_at.isoformat(),
        "late_upload": row.is_late,
        "metrics": diagnostics,
        "queued_hours": db.scalar(
            select(func.count(TelemetryBackfill.id)).where(
                TelemetryBackfill.station_id == station.id
            )
        ),
    }


def escalation_pack(db, station, start, end):
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or not timedelta(0) < end - start <= timedelta(days=31)
    ):
        raise ValueError("Alege un interval de maximum 31 zile, cu fus orar.")
    events, truncated = [], []

    def add_rows(query, kind, extract, time_attr="created_at"):
        rows = db.scalars(
            query.order_by(
                getattr(query.column_descriptions[0]["entity"], time_attr),
                query.column_descriptions[0]["entity"].id,
            ).limit(1001)
        ).all()
        if len(rows) > 1000:
            truncated.append(kind)
        for row in rows[:1000]:
            events.append(
                {
                    "at": getattr(row, time_attr).isoformat(),
                    "kind": kind,
                    "id": str(row.id),
                    **extract(row),
                }
            )

    add_rows(
        select(TelemetryRaw).where(
            TelemetryRaw.station_id == station.id,
            TelemetryRaw.measured_at >= start,
            TelemetryRaw.measured_at < end,
        ),
        "telemetry",
        lambda r: {
            "quality": quality(r, r.measured_at),
            "source": r.source,
            "metrics": {
                k: str(getattr(r, k)) if getattr(r, k) is not None else None
                for k in (
                    "pv_power_w",
                    "load_power_w",
                    "grid_power_w",
                    "battery_power_w",
                    "battery_soc_percent",
                )
            },
            "late_upload": r.is_late,
        },
        "measured_at",
    )
    add_rows(
        select(AlertEvent)
        .join(Alert, Alert.id == AlertEvent.alert_id)
        .where(
            Alert.station_id == station.id,
            AlertEvent.occurred_at >= start,
            AlertEvent.occurred_at < end,
        ),
        "alert",
        lambda r: {"alert_id": str(r.alert_id), "state": r.status},
        "occurred_at",
    )
    add_rows(
        select(CommandEvent)
        .join(Command, Command.id == CommandEvent.command_id)
        .where(
            Command.station_id == station.id,
            CommandEvent.created_at >= start,
            CommandEvent.created_at < end,
        ),
        "command",
        lambda r: {"command_id": str(r.command_id), "state": _inventory(r.event_type)},
    )
    add_rows(
        select(StationConfigVersion).where(
            StationConfigVersion.station_id == station.id,
            StationConfigVersion.created_at >= start,
            StationConfigVersion.created_at < end,
        ),
        "configuration",
        lambda r: {"version": r.version},
    )
    add_rows(
        select(FirmwareDeploymentEvent)
        .join(FirmwareDeployment, FirmwareDeployment.id == FirmwareDeploymentEvent.deployment_id)
        .join(Device, Device.id == FirmwareDeployment.device_id)
        .where(
            Device.station_id == station.id,
            FirmwareDeploymentEvent.created_at >= start,
            FirmwareDeploymentEvent.created_at < end,
        ),
        "firmware",
        lambda r: {"deployment_id": str(r.deployment_id), "state": _inventory(r.event_type)},
    )
    add_rows(
        select(DeviceLogEntry)
        .join(Device, Device.id == DeviceLogEntry.device_id)
        .where(
            Device.station_id == station.id,
            DeviceLogEntry.occurred_at >= start,
            DeviceLogEntry.occurred_at < end,
        ),
        "device_log",
        lambda r: {
            "level": r.level,
            "code": r.code
            if r.code
            in ("read_failed", "upload_failed", "config_failed", "queue_full", "clock_unsynced")
            else "device_event",
            "detail": "[redacted]",
        },
        "occurred_at",
    )
    alerts = db.scalars(
        select(Alert)
        .where(Alert.station_id == station.id, Alert.created_at >= start, Alert.created_at < end)
        .order_by(Alert.created_at, Alert.id)
    ).all()
    resolved = [a for a in alerts if a.resolved_at]
    ack = [a for a in alerts if a.acknowledged_at]
    config = db.scalar(
        select(StationConfigVersion)
        .where(StationConfigVersion.station_id == station.id, StationConfigVersion.created_at < end)
        .order_by(StationConfigVersion.version.desc())
        .limit(1)
    )
    return {
        "schema_version": 1,
        "station_id": str(station.id),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": station.timezone,
        "config_version": config.version if config else None,
        "devices": [
            {"id": str(d.id), "firmware": _inventory(d.firmware_version)}
            for d in db.scalars(
                select(Device).where(Device.station_id == station.id).order_by(Device.id)
            )
        ],
        "alerts": [
            {
                "id": str(a.id),
                "rule": a.category if a.category in RULES else "legacy",
                "status": a.status,
                "severity": a.severity,
                "evidence": sanitized_evidence(a.context),
                "runbook": f"/health/runbooks#{a.category}"
                if a.category in RULES
                else "/health/runbooks",
            }
            for a in alerts
        ],
        "timeline": sorted(events, key=lambda r: (r["at"], r["kind"], r["id"])),
        "truncated": truncated,
        "metrics": {
            "acknowledgement_seconds_mean": sum(
                (a.acknowledged_at - a.created_at).total_seconds() for a in ack
            )
            / len(ack)
            if ack
            else None,
            "resolution_seconds_mean": sum(
                (a.resolved_at - a.created_at).total_seconds() for a in resolved
            )
            / len(resolved)
            if resolved
            else None,
            "feedback_precision": str(
                Decimal(sum(a.status != "false_positive" for a in resolved)) / len(resolved)
            )
            if resolved
            else None,
            "feedback_sample_size": len(resolved),
            "time_to_understand_proxy": "acknowledgement_seconds_mean",
        },
    }
