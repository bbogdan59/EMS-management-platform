from datetime import timedelta

from app.core.security import utcnow
from app.models.task_execution import TaskExecution
from app.services import task_execution_service


def test_available_manual_tasks_are_only_beat_tasks():
    names = {item["name"] for item in task_execution_service.available_manual_tasks()}
    assert "app.workers.tasks.run_aggregation_task" in names
    assert "app.workers.tasks.admin_opcom_import_job_task" not in names
    assert "app.workers.tasks.task_execution_retention_task" not in names


def test_result_failure_detection_catches_partial_worker_failures():
    assert task_execution_service.result_failed({"errors": ["station failed"]}) is True
    assert task_execution_service.result_failed({"failed": 1}) is True
    assert task_execution_service.result_failed({"stages": {"weather": {"failed": 1}}}) is True
    assert task_execution_service.result_failed({"processed": 3, "errors": []}) is False


def test_task_execution_retention_deletes_only_older_than_ten_days(db):
    old = TaskExecution(
        celery_task_id="old-task",
        task_name="task.old",
        source="scheduler",
        status="succeeded",
        input={},
        queued_at=utcnow() - timedelta(days=11),
    )
    recent = TaskExecution(
        celery_task_id="recent-task",
        task_name="task.recent",
        source="scheduler",
        status="failed",
        input={},
        queued_at=utcnow(),
    )
    db.add_all([old, recent])
    db.flush()
    old.created_at = utcnow() - timedelta(days=11)
    db.flush()

    assert task_execution_service.purge_old_executions(db) == 1
    assert db.get(TaskExecution, old.id) is None
    assert db.get(TaskExecution, recent.id) is not None
