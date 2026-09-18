"""Aplicatia Celery, comuna pentru procesele worker si beat (aceeasi baza de
cod ca web-ul, procese separate). Vezi docker-compose.yml / Dockerfile pentru
comenzile de pornire distincte.

IMPORTANT: ruleaza o SINGURA instanta Celery Beat per mediu -- planificarea
duplicata ar dubla importurile/optimizarile. In docker-compose si Railway,
serviciul `scheduler` este singurul care porneste `celery beat`."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure, task_postrun, task_prerun
from sqlalchemy import select

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "ems_platform",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    "aggregate-telemetry-every-15-min": {
        "task": "app.workers.tasks.run_aggregation_task",
        "schedule": crontab(minute="*/15"),
    },
    "opcom-import-hourly": {
        "task": "app.workers.tasks.opcom_import_daily_task",
        "schedule": crontab(minute=5),
    },
    "weather-and-forecasts-every-30-min": {
        "task": "app.workers.tasks.weather_and_forecast_task",
        "schedule": crontab(minute="*/30"),
    },
    "optimization-every-15-min": {
        "task": "app.workers.tasks.optimization_all_stations_task",
        "schedule": crontab(minute="*/15"),
    },
    "dispatch-commands-every-2-min": {
        "task": "app.workers.tasks.command_dispatch_task",
        "schedule": crontab(minute="*/2"),
    },
    "alerts-every-5-min": {
        "task": "app.workers.tasks.alerts_task",
        "schedule": crontab(minute="*/5"),
    },
    # Interval ales conservator (nu verificat impotriva unui rate limit real
    # Deye Cloud -- vezi docs/LIMITATIONS.md); backoff-ul per-conexiune din
    # `deye_cloud_service.poll_connection` reduce oricum frecventa reala dupa
    # esecuri repetate.
    "deye-cloud-poll-every-5-min": {
        "task": "app.workers.tasks.deye_cloud_poll_task",
        "schedule": crontab(minute="*/5"),
    },
    "retention-daily": {
        "task": "app.workers.tasks.retention_task",
        "schedule": crontab(hour=3, minute=0),
    },
    "market-revision-retention-daily": {
        "task": "app.workers.tasks.market_revision_retention_task",
        "schedule": crontab(hour=3, minute=30),
    },
    "task-execution-retention-daily": {
        "task": "app.workers.tasks.task_execution_retention_task",
        "schedule": crontab(hour=4, minute=0),
    },
}


@task_prerun.connect
def record_task_started(task_id=None, task=None, args=None, kwargs=None, **_):
    from app.core.security import utcnow
    from app.database import session_scope
    from app.models.task_execution import TaskExecution

    with session_scope() as db:
        execution = db.scalar(select(TaskExecution).where(TaskExecution.celery_task_id == task_id))
        if execution is None:
            scheduled_tasks = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
            execution = TaskExecution(
                celery_task_id=task_id,
                task_name=task.name,
                source="scheduler" if task.name in scheduled_tasks else "worker",
                status="running",
                input={"args": list(args or ()), "kwargs": kwargs or {}},
                queued_at=utcnow(),
                started_at=utcnow(),
            )
            db.add(execution)
        else:
            execution.status = "running"
            execution.started_at = utcnow()


@task_postrun.connect
def record_task_finished(task_id=None, retval=None, state=None, **_):
    from app.core.security import utcnow
    from app.database import session_scope
    from app.models.task_execution import TaskExecution
    from app.services.task_execution_service import result_failed, serialize_result

    with session_scope() as db:
        execution = db.scalar(select(TaskExecution).where(TaskExecution.celery_task_id == task_id))
        if execution is None:
            return
        execution.output = serialize_result(retval)
        execution.status = "failed" if state == "FAILURE" or result_failed(retval) else "succeeded"
        execution.finished_at = utcnow()


@task_failure.connect
def record_task_failure(task_id=None, exception=None, **_):
    from app.core.security import utcnow
    from app.database import session_scope
    from app.models.task_execution import TaskExecution

    with session_scope() as db:
        execution = db.scalar(select(TaskExecution).where(TaskExecution.celery_task_id == task_id))
        if execution is None:
            return
        execution.status = "failed"
        execution.error_message = f"{type(exception).__name__}: executia a esuat"[:500]
        execution.finished_at = utcnow()
