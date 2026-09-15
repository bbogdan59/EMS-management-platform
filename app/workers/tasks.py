"""Taskuri Celery. Toate sunt idempotente (folosesc upsert-uri sau verifica
starea existenta inainte de a actiona) si folosesc un lock Redis scurt ca sa
evite suprapunerea a doua rulari ale aceluiasi task (ex. daca o rulare
anterioara intarzie peste intervalul de planificare)."""
from __future__ import annotations

import contextlib
import uuid
from datetime import date, datetime, timedelta

import structlog
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import get_settings
from app.core.audit import record_audit
from app.core.rate_limit import get_redis
from app.core.security import utcnow
from app.database import session_scope
from app.models.admin_job import AdminJob
from app.models.alert import Alert
from app.models.device import Device
from app.models.enums import AdminJobStatus, AdminJobType, AlertSeverity, AlertStatus
from app.models.station import Station
from app.services import (
    aggregation_service,
    command_dispatch_service,
    market_retention_service,
    opcom_service,
    optimization_service,
    pv_forecast_service,
    weather_service,
)
from app.services.consumption_forecast_service import (
    ConsumptionForecastError,
    generate_consumption_forecast,
)
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
    """Ruleaza la fiecare 15 minute (vezi `celery_app.beat_schedule`).

    Nu recalculeaza doar sfertul de ora imediat anterior -- reface, idempotent,
    o fereastra recenta (`aggregation_service.RECENT_REAGGREGATION_LOOKBACK`)
    de sferturi de ora + rollup-urile de ora/zi/luna atinse de ele. Asta prinde
    automat telemetria usor intarziata (retry-uri, reconectari scurte ale
    dispozitivelor) fara sa astepte un backfill manual -- vezi
    `aggregation_service.reaggregate_range` pentru un backfill pe un interval
    istoric arbitrar (dupa o intrerupere lunga a unui dispozitiv, de exemplu)."""
    with _task_lock("aggregation") as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        now = utcnow()
        window_start = now - aggregation_service.RECENT_REAGGREGATION_LOOKBACK
        processed = 0
        with session_scope() as db:
            stations = db.scalars(select(Station).where(Station.is_active.is_(True))).all()
            for station in stations:
                aggregation_service.reaggregate_range(db, station, window_start, now)
                processed += 1
        return {"stations_processed": processed, "window_start": window_start.isoformat(), "window_end": now.isoformat()}


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
        stages = {
            "weather": {"succeeded": 0, "failed": 0},
            "pv": {"succeeded": 0, "failed": 0},
            "consumption": {"succeeded": 0, "failed": 0},
        }
        with session_scope() as db:
            stations = db.scalars(select(Station).where(Station.is_active.is_(True))).all()
            for station in stations:
                try:
                    weather_service.refresh_weather_for_station(db, station)
                    stages["weather"]["succeeded"] += 1
                except Exception as exc:
                    stages["weather"]["failed"] += 1
                    errors.append(f"{station.id}: weather: {exc}")
                try:
                    pv_forecast_service.generate_pv_forecast(db, station)
                except Exception as exc:
                    stages["pv"]["failed"] += 1
                    errors.append(f"{station.id}: pv: {exc}")
                else:
                    stages["pv"]["succeeded"] += 1
                try:
                    generate_consumption_forecast(
                        db, station, utcnow(), utcnow() + timedelta(hours=settings.optimization_horizon_hours + 1)
                    )
                except ConsumptionForecastError as exc:
                    stages["consumption"]["failed"] += 1
                    errors.append(f"{station.id}: consumption: {exc}")
                else:
                    stages["consumption"]["succeeded"] += 1
                processed += 1
        return {"stations_processed": processed, "stages": stages, "errors": errors}


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
            # Device-ul sintetic Deye Cloud (issue #43) nu foloseste
            # NICIODATA protocolul web-device de heartbeat -- alerta
            # "offline" ar fi permanenta si falsa. Prospetimea lui reala e
            # `DeyeCloudConnection.last_sync_at/last_sync_status`, verificata
            # separat de `deye_cloud_poll_task`, nu prin heartbeat.
            if device.capabilities.get("deye_cloud"):
                continue
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


@celery_app.task(name="app.workers.tasks.deye_cloud_poll_task")
def deye_cloud_poll_task() -> dict:
    """Polling read-only al conexiunilor Deye Cloud active (issue #43),
    STRICT pe fundal -- nicio cerere web nu asteapta acest apel. Fiecare
    conexiune e procesata intr-o incercare izolata (o eroare la o conexiune
    nu opreste polling-ul celorlalte); backoff-ul si regula de prioritate fata
    de un dispozitiv EMS local activ sunt in `deye_cloud_service.poll_connection`."""
    from app.models.deye_integration import DeyeCloudConnection
    from app.models.enums import DeyeCloudConnectionStatus
    from app.services import deye_cloud_service

    with _task_lock("deye_cloud_poll") as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        results = {"succeeded": 0, "skipped": 0, "failed": 0}
        with session_scope() as db:
            connection_ids = [
                row[0]
                for row in db.execute(
                    select(DeyeCloudConnection.id).where(
                        DeyeCloudConnection.status == DeyeCloudConnectionStatus.connected.value
                    )
                ).all()
            ]
        for connection_id in connection_ids:
            try:
                with session_scope() as db:
                    connection = db.get(DeyeCloudConnection, connection_id)
                    if connection is None:
                        continue
                    outcome = deye_cloud_service.poll_connection(db, connection)
                    results[outcome["status"]] = results.get(outcome["status"], 0) + 1
            except Exception as exc:
                results["failed"] += 1
                logger.error("deye_cloud_poll.unexpected_error", connection_id=str(connection_id), error=str(exc))
        return results


_SAFE_JOB_ERROR = "Operația a eșuat. Consultați logurile folosind ID-ul jobului."


def _mark_admin_job(job_id: uuid.UUID, **fields) -> None:
    """Actualizeaza un `AdminJob` intr-o sesiune proprie, separata de sesiunea
    care a rulat munca efectiva -- astfel incat un rollback al muncii (ex. o
    exceptie in mijlocul importului OPCOM) sa nu stearga si actualizarea de
    stare pe care vrem sa o pastram (ex. status=failed)."""
    with session_scope() as db:
        job = db.get(AdminJob, job_id)
        if job is None:
            logger.warning("admin_job.not_found", job_id=str(job_id))
            return
        for key, value in fields.items():
            setattr(job, key, value)


def _fail_admin_job(job_id: uuid.UUID, claimed: dict, action: str) -> None:
    with session_scope() as db:
        job = db.get(AdminJob, job_id)
        if job is None:
            logger.warning("admin_job.not_found", job_id=str(job_id))
            return
        job.status = AdminJobStatus.failed.value
        job.finished_at = utcnow()
        job.error_message = _SAFE_JOB_ERROR
        record_audit(
            db,
            action=action,
            resource_type="admin_job",
            resource_id=str(job_id),
            actor_user_id=claimed["triggered_by_user_id"],
            actor_label="admin_job",
            station_id=claimed["station_id"],
            outcome="failure",
        )


def _claim_admin_job(job_id: uuid.UUID, expected_type: str) -> dict | None:
    """Claim atomic pentru livrarea Celery at-least-once.

    Un retry/redelivery al aceluiasi mesaj nu trebuie sa reporneasca o operatie
    deja finalizata. ``FOR UPDATE`` serializeaza worker-ele care primesc
    accidental acelasi task.
    """
    with session_scope() as db:
        job = db.scalar(select(AdminJob).where(AdminJob.id == job_id).with_for_update())
        if job is None:
            logger.warning("admin_job.not_found", job_id=str(job_id))
            return None
        if job.status != AdminJobStatus.queued.value:
            logger.info("admin_job.delivery_ignored", job_id=str(job_id), status=job.status)
            return None
        if job.job_type != expected_type:
            job.status = AdminJobStatus.failed.value
            job.finished_at = utcnow()
            job.error_message = _SAFE_JOB_ERROR
            logger.error("admin_job.type_mismatch", job_id=str(job_id))
            return None

        job.status = AdminJobStatus.running.value
        job.started_at = utcnow()
        return {
            "params": dict(job.params),
            "station_id": job.station_id,
            "triggered_by_user_id": job.triggered_by_user_id,
        }


@celery_app.task(name="app.workers.tasks.admin_opcom_import_job_task")
def admin_opcom_import_job_task(job_id: str) -> dict:
    """Executa, pe fundal, un import OPCOM declansat manual din panoul admin
    (`AdminJob.job_type == 'opcom_import'`) -- ruta web doar creeaza randul
    `AdminJob` (status=queued) si trimite acest task, ca sa nu tina cererea
    HTTP blocata pe durata importului. Nu inlocuieste `opcom_import_daily_task`
    (rularea planificata Celery beat), care ramane neschimbata."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        logger.error("admin_job.invalid_id")
        return {"status": "ignored"}
    claimed = _claim_admin_job(job_uuid, AdminJobType.opcom_import.value)
    if claimed is None:
        return {"status": "ignored"}

    try:
        delivery_date = date.fromisoformat(claimed["params"]["delivery_date"])
        triggered_by_user_id = claimed["triggered_by_user_id"]
        with session_scope() as db:
            run = opcom_service.import_opcom_day(db, delivery_date, triggered_by_user_id=triggered_by_user_id)
            run_id, run_status = run.id, run.status
            record_audit(
                db, action="opcom_import_triggered", resource_type="import_run", resource_id=str(run_id),
                actor_user_id=triggered_by_user_id, actor_label="admin_job",
                metadata={"delivery_date": delivery_date.isoformat(), "status": run_status, "admin_job_id": job_id},
            )
    except Exception as exc:
        logger.error("admin_opcom_import_job.failed", job_id=job_id, exception_type=type(exc).__name__)
        _fail_admin_job(job_uuid, claimed, "opcom_import_failed")
        return {"status": "failed"}

    _mark_admin_job(
        job_uuid,
        status=AdminJobStatus.succeeded.value,
        finished_at=utcnow(),
        result_resource_type="import_run",
        result_resource_id=run_id,
    )
    return {"status": "succeeded", "import_run_id": str(run_id)}


@celery_app.task(name="app.workers.tasks.admin_optimize_station_job_task")
def admin_optimize_station_job_task(job_id: str) -> dict:
    """Executa, pe fundal, o reoptimizare a unei statii declansata manual din
    panoul admin (`AdminJob.job_type == 'optimization'`) -- vezi docstring-ul
    `admin_opcom_import_job_task` pentru motivatie. Nu inlocuieste
    `optimization_all_stations_task` (rularea planificata pentru toate
    statiile), care ramane neschimbata."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        logger.error("admin_job.invalid_id")
        return {"status": "ignored"}
    claimed = _claim_admin_job(job_uuid, AdminJobType.optimization.value)
    if claimed is None:
        return {"status": "ignored"}

    try:
        station_id = claimed["station_id"]
        triggered_by_user_id = claimed["triggered_by_user_id"]
        if station_id is None:
            raise ValueError("Station missing for optimization job")
        with session_scope() as db:
            run = optimization_service.run_optimization_for_station(
                db, station_id, triggered_by="user", triggered_by_user_id=triggered_by_user_id
            )
            run_id, run_status = run.id, run.status
            record_audit(
                db, action="optimization_triggered", resource_type="optimization_run", resource_id=str(run_id),
                actor_user_id=triggered_by_user_id, actor_label="admin_job", station_id=station_id,
                metadata={"status": run_status, "admin_job_id": job_id},
            )
    except OptimizationLockedError:
        _mark_admin_job(job_uuid, status=AdminJobStatus.skipped_locked.value, finished_at=utcnow())
        return {"status": "skipped_locked"}
    except Exception as exc:
        logger.error("admin_optimize_station_job.failed", job_id=job_id, exception_type=type(exc).__name__)
        _fail_admin_job(job_uuid, claimed, "optimization_failed")
        return {"status": "failed"}

    _mark_admin_job(
        job_uuid,
        status=AdminJobStatus.succeeded.value,
        finished_at=utcnow(),
        result_resource_type="optimization_run",
        result_resource_id=run_id,
    )
    return {"status": "succeeded", "optimization_run_id": str(run_id)}


@celery_app.task(name="app.workers.tasks.admin_market_retention_job_task")
def admin_market_retention_job_task(job_id: str) -> dict:
    """Executa, pe fundal, arhivarea reviziilor OPCOM excedentare declansata
    manual din panoul admin (`AdminJob.job_type == 'market_retention'`) --
    vezi docstring-ul `admin_opcom_import_job_task` pentru motivatie. Nu
    inlocuieste `market_revision_retention_task` (rularea planificata
    zilnica), care ramane neschimbata. STRICT NEDISTRUCTIV -- vezi
    `market_retention_service` pentru politica."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        logger.error("admin_job.invalid_id")
        return {"status": "ignored"}
    claimed = _claim_admin_job(job_uuid, AdminJobType.market_retention.value)
    if claimed is None:
        return {"status": "ignored"}

    try:
        params = claimed["params"]
        dry_run = bool(params.get("dry_run", False))
        max_active_revisions = int(params.get("max_active_revisions", market_retention_service.DEFAULT_MAX_ACTIVE_REVISIONS))
        triggered_by_user_id = claimed["triggered_by_user_id"]
        with session_scope() as db:
            result = market_retention_service.enforce_revision_retention(
                db, max_active_revisions=max_active_revisions, dry_run=dry_run
            )
            record_audit(
                db, action="market_retention_triggered", resource_type="admin_job", resource_id=job_id,
                actor_user_id=triggered_by_user_id, actor_label="admin_job",
                metadata={"admin_job_id": job_id, **result.as_dict()},
            )
    except Exception as exc:
        logger.error("admin_market_retention_job.failed", job_id=job_id, exception_type=type(exc).__name__)
        _fail_admin_job(job_uuid, claimed, "market_retention_failed")
        return {"status": "failed"}

    _mark_admin_job(
        job_uuid,
        status=AdminJobStatus.succeeded.value,
        finished_at=utcnow(),
        params={**params, "result": result.as_dict()},
    )
    return {"status": "succeeded", **result.as_dict()}


@celery_app.task(name="app.workers.tasks.market_revision_retention_task")
def market_revision_retention_task() -> dict:
    """Rulare planificata zilnica a retentiei de revizii OPCOM (issue #51) --
    STRICT NEDISTRUCTIVA (vezi `market_retention_service`), separata de
    `retention_task` de mai jos (acela FACE hard-delete pentru telemetrie/
    audit dupa varsta, politica total diferita, nu trebuie confundate)."""
    with _task_lock("market_revision_retention") as acquired:
        if not acquired:
            return {"status": "skipped_locked"}
        with session_scope() as db:
            result = market_retention_service.enforce_revision_retention(db, dry_run=False)
    return {"status": "succeeded", **result.as_dict()}


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
