"""Taskuri Celery. Toate sunt idempotente (folosesc upsert-uri sau verifica
starea existenta inainte de a actiona) si folosesc un lock Redis scurt ca sa
evite suprapunerea a doua rulari ale aceluiasi task (ex. daca o rulare
anterioara intarzie peste intervalul de planificare)."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import get_settings
from app.core.rate_limit import get_redis
from app.core.security import utcnow
from app.database import session_scope
from app.models.alert import Alert
from app.models.device import Device
from app.models.enums import AlertSeverity, AlertStatus
from app.models.station import Station
from app.services import aggregation_service, command_dispatch_service, opcom_service, optimization_service, pv_forecast_service, weather_service
from app.services.consumption_forecast_service import ConsumptionForecastError, generate_consumption_forecast
from app.services.optimization_service import OptimizationLockedError

logger = structlog.get_logger(__name__)
settings = get_settings()


@contextlib.contextmanager
def _task_lock(name: str, timeout: int = 600):
    r = get_redis()
    lock = r.lock(f"task_lock:{name}", timeout=timeout, blocking_timeout=1)
    acquired = lock.acquire(blocking=True)
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                lock.release()


@celery_app.task(name="app.workers.tasks.run_aggregation_task")
def run_aggregation_task() -> dict:
    with _task_lock("aggregation") as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        now = utcnow()
        interval_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) - timedelta(minutes=15)
        processed = 0
        with session_scope() as db:
            station_ids = [row[0] for row in db.execute(select(Station.id)).all()]
            for sid in station_ids:
                aggregation_service.aggregate_interval_15m(db, sid, interval_start)
                if now.minute < 15:
                    aggregation_service.aggregate_hour(db, sid, (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
                if now.hour == 0 and now.minute < 15:
                    aggregation_service.aggregate_day(db, sid, (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
                if now.day == 1 and now.hour == 0 and now.minute < 15:
                    prev_month_end = now.replace(day=1) - timedelta(days=1)
                    aggregation_service.aggregate_month(db, sid, prev_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
                processed += 1
        return {"stations_processed": processed, "interval_start": interval_start.isoformat()}


@celery_app.task(name="app.workers.tasks.opcom_import_daily_task")
def opcom_import_daily_task() -> dict:
    with _task_lock("opcom_import") as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        results = {}
        with session_scope() as db:
            for offset in (0, 1):
                d = datetime.now(opcom_service.BUCHAREST).date() + timedelta(days=offset)
                if opcom_service.has_successful_real_import(db, d):
                    results[d.isoformat()] = "already_succeeded"
                    continue
                run = opcom_service.import_opcom_day(db, d)
                results[d.isoformat()] = run.status
        return results


@celery_app.task(name="app.workers.tasks.weather_and_forecast_task")
def weather_and_forecast_task() -> dict:
    with _task_lock("weather_forecast") as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        processed, errors = 0, []
        with session_scope() as db:
            stations = db.scalars(select(Station).where(Station.is_active.is_(True))).all()
            for station in stations:
                try:
                    weather_service.refresh_weather_for_station(db, station)
                    pv_forecast_service.generate_pv_forecast(db, station)
                except Exception as exc:
                    errors.append(f"{station.id}: {exc}")
                try:
                    generate_consumption_forecast(
                        db, station, utcnow(), utcnow() + timedelta(hours=settings.optimization_horizon_hours + 1)
                    )
                except ConsumptionForecastError as exc:
                    errors.append(f"{station.id}: {exc}")
                processed += 1
        return {"stations_processed": processed, "errors": errors}


@celery_app.task(name="app.workers.tasks.optimization_all_stations_task")
def optimization_all_stations_task() -> dict:
    processed, skipped, errors = 0, 0, []
    with session_scope() as db:
        station_ids = [row[0] for row in db.execute(select(Station.id).where(Station.is_active.is_(True))).all()]
    for sid in station_ids:
        try:
            with session_scope() as db:
                optimization_service.run_optimization_for_station(db, sid, triggered_by="scheduler")
            processed += 1
        except OptimizationLockedError:
            skipped += 1
        except Exception as exc:
            errors.append(f"{sid}: {exc}")
            logger.error("optimization_task.error", station_id=str(sid), error=str(exc))
    return {"processed": processed, "skipped_locked": skipped, "errors": errors}


@celery_app.task(name="app.workers.tasks.command_dispatch_task")
def command_dispatch_task() -> dict:
    with session_scope() as db:
        created = command_dispatch_service.dispatch_due_commands(db)
        return {"commands_created": len(created)}


@celery_app.task(name="app.workers.tasks.alerts_task")
def alerts_task() -> dict:
    now = utcnow()
    created = 0
    with session_scope() as db:
        devices = db.scalars(select(Device)).all()
        for device in devices:
            is_offline = device.status == "active" and (
                device.last_heartbeat_at is None or (now - device.last_heartbeat_at) > timedelta(minutes=15)
            )
            open_alerts_for_station = db.scalars(
                select(Alert).where(
                    Alert.station_id == device.station_id,
                    Alert.category == "device_offline",
                    Alert.status == AlertStatus.open.value,
                )
            ).all()
            open_alert = next((a for a in open_alerts_for_station if a.context.get("device_id") == str(device.id)), None)
            if is_offline and open_alert is None:
                db.add(
                    Alert(
                        station_id=device.station_id,
                        category="device_offline",
                        severity=AlertSeverity.warning.value,
                        status=AlertStatus.open.value,
                        title=f"Dispozitiv offline: {device.name}",
                        description="Niciun heartbeat primit in ultimele 15 minute.",
                        context={"device_id": str(device.id)},
                    )
                )
                created += 1
            elif not is_offline and open_alert is not None:
                open_alert.status = AlertStatus.resolved.value
                open_alert.resolved_at = now
                db.add(open_alert)
    return {"alerts_created": created}


@celery_app.task(name="app.workers.tasks.retention_task")
def retention_task() -> dict:
    from app.models.audit import AuditLog
    from app.models.telemetry import TelemetryAggregate, TelemetryRaw

    now = utcnow()
    with session_scope() as db:
        raw_cutoff = now - timedelta(days=settings.telemetry_raw_retention_days)
        agg_cutoff = now - timedelta(days=settings.telemetry_aggregate_retention_days)
        audit_cutoff = now - timedelta(days=settings.audit_log_retention_days)

        raw_deleted = db.query(TelemetryRaw).filter(TelemetryRaw.measured_at < raw_cutoff).delete(synchronize_session=False)
        agg_deleted = db.query(TelemetryAggregate).filter(TelemetryAggregate.period_start < agg_cutoff).delete(synchronize_session=False)
        audit_deleted = db.query(AuditLog).filter(AuditLog.occurred_at < audit_cutoff).delete(synchronize_session=False)

    return {"raw_deleted": raw_deleted, "aggregates_deleted": agg_deleted, "audit_deleted": audit_deleted}
