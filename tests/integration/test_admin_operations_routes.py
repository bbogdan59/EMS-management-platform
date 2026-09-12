"""Teste HTTP pentru rutele de declansare a joburilor admin asincrone
(issue #11): `POST /admin/operations/import-opcom` si
`POST /admin/operations/optimize/{station_id}`.

Aceste teste folosesc fixture-ul `client`/`db` obisnuit (izolat prin
SAVEPOINT pe o singura conexiune) si NU lasa taskul Celery sa ruleze cu
adevarat -- `.delay()` este inlocuit cu un stub care doar retine argumentele.
Motivul: taskurile reale deschid propria sesiune (`session_scope()`, o
conexiune noua), care nu ar vedea datele create in sesiunea de test izolata
prin SAVEPOINT (vezi `tests/integration/test_admin_job_tasks.py` pentru
testarea taskurilor insele, cu date comise real prin fixture-ul `engine`).
Aici testam doar comportamentul HTTP: crearea randului `AdminJob`, protectia
la declansare duplicata si redirect-urile."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.admin_job import AdminJob
from app.models.enums import AdminJobStatus
from app.workers import tasks as tasks_module
from tests.factories import make_org, make_station, make_user
from tests.web_helpers import login


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    reset_key("login_attempts:testclient")


class _FakeAsyncResult:
    def __init__(self, task_id: str):
        self.id = task_id


def _stub_delay(monkeypatch, task, calls: list):
    def _fake_delay(*args, **kwargs):
        calls.append(args)
        return _FakeAsyncResult("fake-task-id")

    monkeypatch.setattr(task, "delay", _fake_delay)


def test_trigger_opcom_import_creates_queued_admin_job(client, db, monkeypatch):
    admin = make_user(db, email="opsjob1@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_opcom_import_job_task, calls)

    login(client, "opsjob1@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/operations/import-opcom",
        data={"csrf_token": csrf, "delivery_date": "2026-01-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations"
    assert len(calls) == 1

    job = db.scalar(select(AdminJob).where(AdminJob.triggered_by_user_id == admin.id))
    assert job is not None
    assert job.status == AdminJobStatus.queued.value
    assert job.celery_task_id == "fake-task-id"
    assert job.params["delivery_date"] == "2026-01-15"


def test_trigger_opcom_import_rejects_duplicate_in_progress(client, db, monkeypatch):
    admin = make_user(db, email="opsjob2@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_opcom_import_job_task, calls)

    login(client, "opsjob2@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp1 = client.post(
        "/admin/operations/import-opcom", data={"csrf_token": csrf, "delivery_date": "2026-02-01"}, follow_redirects=False
    )
    assert resp1.status_code == 303

    resp2 = client.post(
        "/admin/operations/import-opcom", data={"csrf_token": csrf, "delivery_date": "2026-02-01"}, follow_redirects=False
    )
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/admin/operations?error=opcom_import_in_progress"
    assert len(calls) == 1

    jobs = db.scalars(select(AdminJob).where(AdminJob.triggered_by_user_id == admin.id)).all()
    assert len(jobs) == 1


def test_trigger_opcom_import_rejects_invalid_date(client, db):
    make_user(db, email="opsjob-invalid-date@test.local", password="Password1234", is_platform_admin=True)
    db.commit()
    login(client, "opsjob-invalid-date@test.local", "Password1234")

    response = client.post(
        "/admin/operations/import-opcom",
        data={"csrf_token": client.cookies.get("ems_csrf"), "delivery_date": "not-a-date"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/operations?error=invalid_delivery_date"


def test_enqueue_failure_marks_job_failed_and_allows_retry(client, db, monkeypatch):
    admin = make_user(db, email="opsjob-enqueue-fail@test.local", password="Password1234", is_platform_admin=True)
    db.commit()
    login(client, admin.email, "Password1234")

    def _fail_delay(*args, **kwargs):
        raise RuntimeError("broker at redis://user:secret@internal unavailable")

    monkeypatch.setattr(tasks_module.admin_opcom_import_job_task, "delay", _fail_delay)
    response = client.post(
        "/admin/operations/import-opcom",
        data={"csrf_token": client.cookies.get("ems_csrf"), "delivery_date": "2026-02-02"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/operations?error=enqueue_failed"
    job = db.scalar(select(AdminJob).where(AdminJob.triggered_by_user_id == admin.id))
    assert job.status == AdminJobStatus.failed.value
    assert "secret" not in job.error_message


def test_trigger_optimization_creates_queued_admin_job(client, db, monkeypatch):
    admin = make_user(db, email="opsjob3@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "OpsJob Org 3")
    station = make_station(db, org, admin, name="OpsJob Station 3")
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_optimize_station_job_task, calls)

    login(client, "opsjob3@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations"
    assert len(calls) == 1

    job = db.scalar(select(AdminJob).where(AdminJob.station_id == station.id))
    assert job is not None
    assert job.status == AdminJobStatus.queued.value
    assert job.target_label == station.name


def test_trigger_optimization_rejects_duplicate_in_progress(client, db, monkeypatch):
    admin = make_user(db, email="opsjob4@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "OpsJob Org 4")
    station = make_station(db, org, admin, name="OpsJob Station 4")
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_optimize_station_job_task, calls)

    login(client, "opsjob4@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp1 = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp1.status_code == 303

    resp2 = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/admin/operations?error=optimization_in_progress"
    assert len(calls) == 1


def test_trigger_optimization_unknown_station_returns_error(client, db):
    make_user(db, email="opsjob5@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    login(client, "opsjob5@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(f"/admin/operations/optimize/{uuid.uuid4()}", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations?error=station_not_found"


def test_operations_page_renders_admin_jobs(client, db, monkeypatch):
    make_user(db, email="opsjob6@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_opcom_import_job_task, calls)

    login(client, "opsjob6@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")
    client.post(
        "/admin/operations/import-opcom", data={"csrf_token": csrf, "delivery_date": "2026-03-01"}, follow_redirects=False
    )

    resp = client.get("/admin/operations")
    assert resp.status_code == 200
    assert "Import OPCOM 2026-03-01" in resp.text


def test_trigger_market_retention_creates_queued_admin_job_defaulting_to_dry_run(client, db, monkeypatch):
    admin = make_user(db, email="opsjob-retention1@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_market_retention_job_task, calls)

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/operations/market-retention",
        data={"csrf_token": csrf, "max_active_revisions": "5", "dry_run": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations"
    assert len(calls) == 1

    job = db.scalar(select(AdminJob).where(AdminJob.triggered_by_user_id == admin.id))
    assert job is not None
    assert job.job_type == "market_retention"
    assert job.params["dry_run"] is True
    assert job.params["max_active_revisions"] == 5


def test_trigger_market_retention_without_dry_run_checkbox_runs_for_real(client, db, monkeypatch):
    admin = make_user(db, email="opsjob-retention2@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_market_retention_job_task, calls)

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        "/admin/operations/market-retention",
        data={"csrf_token": csrf, "max_active_revisions": "5"},  # fara "dry_run" -- checkbox nebifat
        follow_redirects=False,
    )
    assert resp.status_code == 303

    job = db.scalar(select(AdminJob).where(AdminJob.triggered_by_user_id == admin.id))
    assert job.params["dry_run"] is False


def test_trigger_market_retention_rejects_invalid_max_active_revisions(client, db):
    make_user(db, email="opsjob-retention3@test.local", password="Password1234", is_platform_admin=True)
    db.commit()
    login(client, "opsjob-retention3@test.local", "Password1234")

    resp = client.post(
        "/admin/operations/market-retention",
        data={"csrf_token": client.cookies.get("ems_csrf"), "max_active_revisions": "0", "dry_run": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations?error=invalid_max_active_revisions"


def test_trigger_market_retention_rejects_duplicate_in_progress(client, db, monkeypatch):
    admin = make_user(db, email="opsjob-retention4@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    calls: list = []
    _stub_delay(monkeypatch, tasks_module.admin_market_retention_job_task, calls)

    login(client, admin.email, "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp1 = client.post(
        "/admin/operations/market-retention",
        data={"csrf_token": csrf, "max_active_revisions": "5", "dry_run": "true"},
        follow_redirects=False,
    )
    assert resp1.status_code == 303

    resp2 = client.post(
        "/admin/operations/market-retention",
        data={"csrf_token": csrf, "max_active_revisions": "5", "dry_run": "true"},
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/admin/operations?error=market_retention_in_progress"
    assert len(calls) == 1
