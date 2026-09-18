from __future__ import annotations

import json
from datetime import timedelta

from celery.result import AsyncResult
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.security import utcnow
from app.models.task_execution import TaskExecution

RETENTION_DAYS = 10
MAX_OUTPUT_CHARS = 20_000


def available_manual_tasks() -> list[dict[str, str]]:
    return [
        {"name": entry["task"], "label": schedule_name, "schedule": str(entry["schedule"])}
        for schedule_name, entry in sorted(celery_app.conf.beat_schedule.items())
        if entry["task"] != "app.workers.tasks.task_execution_retention_task"
    ]


def queue_manual_task(db: Session, task_name: str, user_id) -> tuple[TaskExecution, AsyncResult]:
    allowed = {item["name"] for item in available_manual_tasks()}
    if task_name not in allowed:
        raise ValueError("task_not_allowed")
    now = utcnow()
    execution = TaskExecution(
        celery_task_id="pending",
        task_name=task_name,
        source="manual",
        status="queued",
        input={},
        triggered_by_user_id=user_id,
        queued_at=now,
    )
    db.add(execution)
    db.flush()
    execution.celery_task_id = str(execution.id)
    db.commit()
    try:
        result = celery_app.send_task(task_name, task_id=execution.celery_task_id)
    except Exception:
        execution.status = "failed"
        execution.finished_at = utcnow()
        execution.error_message = "Taskul nu a putut fi trimis catre worker."
        db.commit()
        raise
    return execution, result


def serialize_result(value):
    try:
        encoded = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = json.dumps(str(value), ensure_ascii=False)
    if len(encoded) > MAX_OUTPUT_CHARS:
        return {"truncated": True, "preview": encoded[:MAX_OUTPUT_CHARS]}
    return json.loads(encoded)


def result_failed(value) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("errors"):
        return True
    if isinstance(value.get("failed"), int) and value["failed"] > 0:
        return True
    stages = value.get("stages")
    return isinstance(stages, dict) and any(isinstance(stage, dict) and stage.get("failed", 0) > 0 for stage in stages.values())


def purge_old_executions(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    result = db.execute(delete(TaskExecution).where(TaskExecution.created_at < cutoff))
    return result.rowcount or 0


def failed_count(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    return db.scalar(select(func.count(TaskExecution.id)).where(TaskExecution.status == "failed", TaskExecution.created_at >= cutoff)) or 0
