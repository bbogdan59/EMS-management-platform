"""Teste pentru cele doua taskuri Celery de fundal declansate din panoul admin
(issue #11): `admin_opcom_import_job_task` / `admin_optimize_station_job_task`.

Aceste taskuri folosesc `session_scope()` intern -- adica o sesiune SQLAlchemy
NOUA, legata de o conexiune reala, separata de sesiunea de test obisnuita
(fixture-ul `db`, care ruleaza intr-un SAVEPOINT nerulat niciodata pana la
capat -- vizibil doar pe aceeasi conexiune). Ca sa vada datele de setup,
aceste teste folosesc deci fixture-ul `engine` direct + o sesiune proprie cu
`.commit()` real, exact ca testele de concurenta din
`tests/integration/test_device_enrollment.py`."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app.core.rate_limit import get_redis
from app.core.security import utcnow
from app.models.admin_job import AdminJob
from app.models.enums import AdminJobStatus, AdminJobType
from app.models.market import ImportRun
from app.models.optimization import OptimizationRun
from app.models.tariff import Tariff, TariffVersion
from app.services import opcom_service, optimization_service
from app.workers.tasks import admin_opcom_import_job_task, admin_optimize_station_job_task
from tests.factories import make_org, make_station, make_user


def _session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _cleanup(engine, *, admin_job_ids=(), station_id=None, org_id=None, user_id=None):
    """Sterge datele create de test folosind DELETE de nivel Core (nu
    `session.delete()` ORM): `Organization.stations` nu are `cascade="delete"`
    configurat pe partea ORM (doar `ondelete="CASCADE"` la nivel de schema),
    deci un `session.delete(org)` ar incerca sa orfanizeze statiile (SET
    organization_id=NULL) in loc sa le stearga -- ceea ce incalca `nullable
    =False`. Un DELETE Core simplu lasa toata cascada in seama Postgres.

    `audit_logs` si `import_runs` au FK-uri catre `users` FARA cascada (jurnal
    de audit / istoric de import -- pastrate intentionat chiar daca statia
    dispare), deci le stergem explicit. Organizatia (cascadeaza spre
    Station/config/preferences/Membership/OptimizationRun) se sterge
    INTR-O TRANZACTIE SEPARATA de utilizator, ca sa fie sigur ca cascada s-a
    incheiat inainte de a sterge randul din `users`."""
    from app.models.audit import AuditLog

    Session = _session_factory(engine)

    db = Session()
    try:
        if admin_job_ids:
            db.execute(delete(AdminJob).where(AdminJob.id.in_(admin_job_ids)))
        if user_id is not None:
            db.execute(delete(AuditLog).where(AuditLog.actor_user_id == user_id))
            db.execute(delete(ImportRun).where(ImportRun.triggered_by_user_id == user_id))
        if org_id is not None:
            from app.models.organization import Organization

            db.execute(delete(Organization).where(Organization.id == org_id))
        db.commit()
    finally:
        db.close()

    if user_id is not None:
        from app.models.user import User

        db2 = Session()
        try:
            db2.execute(delete(User).where(User.id == user_id))
            db2.commit()
        finally:
            db2.close()


def test_admin_opcom_import_job_task_succeeds(engine):
    Session = _session_factory(engine)
    setup = Session()
    delivery_date = date.today().isoformat()
    try:
        user = make_user(setup, email=f"admjob-opcom-{uuid.uuid4().hex[:8]}@test.local", is_platform_admin=True)
        setup.commit()
        user_id = user.id
    finally:
        setup.close()

    job_setup = Session()
    try:
        job = AdminJob(
            job_type=AdminJobType.opcom_import.value,
            status=AdminJobStatus.queued.value,
            params={"delivery_date": delivery_date},
            target_label=f"Import OPCOM {delivery_date}",
            triggered_by_user_id=user_id,
        )
        job_setup.add(job)
        job_setup.commit()
        job_id = job.id
    finally:
        job_setup.close()

    try:
        result = admin_opcom_import_job_task(str(job_id))
        assert result["status"] == "succeeded"

        verify = Session()
        try:
            refreshed = verify.get(AdminJob, job_id)
            assert refreshed.status == AdminJobStatus.succeeded.value
            assert refreshed.started_at is not None
            assert refreshed.finished_at is not None
            assert refreshed.result_resource_type == "import_run"
            assert refreshed.result_resource_id is not None
            run = verify.get(ImportRun, refreshed.result_resource_id)
            assert run is not None
            assert run.status == "succeeded"
        finally:
            verify.close()
    finally:
        _cleanup(engine, admin_job_ids=[job_id], user_id=user_id)


def test_admin_opcom_import_job_task_failure_sets_safe_error(engine, monkeypatch):
    Session = _session_factory(engine)
    delivery_date = date.today().isoformat()
    setup = Session()
    try:
        user = make_user(setup, email=f"admjob-opcom-fail-{uuid.uuid4().hex[:8]}@test.local", is_platform_admin=True)
        setup.commit()
        user_id = user.id
    finally:
        setup.close()

    job_setup = Session()
    try:
        job = AdminJob(
            job_type=AdminJobType.opcom_import.value,
            status=AdminJobStatus.queued.value,
            params={"delivery_date": delivery_date},
            target_label=f"Import OPCOM {delivery_date}",
            triggered_by_user_id=user_id,
        )
        job_setup.add(job)
        job_setup.commit()
        job_id = job.id
    finally:
        job_setup.close()

    def _boom(*args, **kwargs):
        raise ValueError("eroare simulata de import OPCOM")

    monkeypatch.setattr(opcom_service, "import_opcom_day", _boom)

    try:
        result = admin_opcom_import_job_task(str(job_id))
        assert result["status"] == "failed"

        verify = Session()
        try:
            refreshed = verify.get(AdminJob, job_id)
            assert refreshed.status == AdminJobStatus.failed.value
            assert refreshed.finished_at is not None
            assert refreshed.error_message == "eroare simulata de import OPCOM"
            assert len(refreshed.error_message) <= 500
        finally:
            verify.close()
    finally:
        _cleanup(engine, admin_job_ids=[job_id], user_id=user_id)


def _add_tariffs(db, station):
    for direction, price in (("import", "0.9"), ("export", "0.35")):
        t = Tariff(station_id=station.id, direction=direction, kind="fixed", name=f"t-{direction}")
        db.add(t)
        db.flush()
        db.add(
            TariffVersion(
                tariff_id=t.id, valid_from=utcnow() - timedelta(days=1), fixed_price_lei_per_kwh=Decimal(price),
                fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
            )
        )
    db.flush()


def test_admin_optimize_station_job_task_succeeds(engine):
    Session = _session_factory(engine)
    setup = Session()
    try:
        user = make_user(setup, email=f"admjob-opt-{uuid.uuid4().hex[:8]}@test.local", is_platform_admin=True)
        org = make_org(setup, name=f"AdminJob Opt Org {uuid.uuid4().hex[:8]}")
        station = make_station(setup, org, user, name=f"AdminJob Opt Station {uuid.uuid4().hex[:8]}")
        _add_tariffs(setup, station)
        setup.commit()
        user_id, org_id, station_id = user.id, org.id, station.id
    finally:
        setup.close()

    job_setup = Session()
    try:
        job = AdminJob(
            job_type=AdminJobType.optimization.value,
            status=AdminJobStatus.queued.value,
            params={},
            target_label="AdminJob Opt Station",
            station_id=station_id,
            triggered_by_user_id=user_id,
        )
        job_setup.add(job)
        job_setup.commit()
        job_id = job.id
    finally:
        job_setup.close()

    try:
        result = admin_optimize_station_job_task(str(job_id))
        assert result["status"] == "succeeded"

        verify = Session()
        try:
            refreshed = verify.get(AdminJob, job_id)
            assert refreshed.status == AdminJobStatus.succeeded.value
            assert refreshed.result_resource_type == "optimization_run"
            assert refreshed.result_resource_id is not None
            run = verify.get(OptimizationRun, refreshed.result_resource_id)
            assert run is not None
        finally:
            verify.close()
    finally:
        _cleanup(engine, admin_job_ids=[job_id], org_id=org_id, user_id=user_id)


def test_admin_optimize_station_job_task_skipped_when_locked(engine):
    Session = _session_factory(engine)
    setup = Session()
    try:
        user = make_user(setup, email=f"admjob-optlock-{uuid.uuid4().hex[:8]}@test.local", is_platform_admin=True)
        org = make_org(setup, name=f"AdminJob Lock Org {uuid.uuid4().hex[:8]}")
        station = make_station(setup, org, user, name=f"AdminJob Lock Station {uuid.uuid4().hex[:8]}")
        setup.commit()
        user_id, org_id, station_id = user.id, org.id, station.id
    finally:
        setup.close()

    job_setup = Session()
    try:
        job = AdminJob(
            job_type=AdminJobType.optimization.value,
            status=AdminJobStatus.queued.value,
            params={},
            target_label="AdminJob Lock Station",
            station_id=station_id,
            triggered_by_user_id=user_id,
        )
        job_setup.add(job)
        job_setup.commit()
        job_id = job.id
    finally:
        job_setup.close()

    r = get_redis()
    lock = r.lock(f"optimization_lock:{station_id}", timeout=30)
    assert lock.acquire(blocking=False)
    try:
        result = admin_optimize_station_job_task(str(job_id))
        assert result["status"] == "skipped_locked"

        verify = Session()
        try:
            refreshed = verify.get(AdminJob, job_id)
            assert refreshed.status == AdminJobStatus.skipped_locked.value
            assert refreshed.finished_at is not None
        finally:
            verify.close()
    finally:
        lock.release()
        _cleanup(engine, admin_job_ids=[job_id], org_id=org_id, user_id=user_id)


def test_admin_optimize_station_job_task_failure_sets_safe_error(engine, monkeypatch):
    Session = _session_factory(engine)
    setup = Session()
    try:
        user = make_user(setup, email=f"admjob-optfail-{uuid.uuid4().hex[:8]}@test.local", is_platform_admin=True)
        org = make_org(setup, name=f"AdminJob Fail Org {uuid.uuid4().hex[:8]}")
        station = make_station(setup, org, user, name=f"AdminJob Fail Station {uuid.uuid4().hex[:8]}")
        setup.commit()
        user_id, org_id, station_id = user.id, org.id, station.id
    finally:
        setup.close()

    job_setup = Session()
    try:
        job = AdminJob(
            job_type=AdminJobType.optimization.value,
            status=AdminJobStatus.queued.value,
            params={},
            target_label="AdminJob Fail Station",
            station_id=station_id,
            triggered_by_user_id=user_id,
        )
        job_setup.add(job)
        job_setup.commit()
        job_id = job.id
    finally:
        job_setup.close()

    def _boom(*args, **kwargs):
        raise ValueError("eroare simulata de optimizare")

    monkeypatch.setattr(optimization_service, "run_optimization_for_station", _boom)

    try:
        result = admin_optimize_station_job_task(str(job_id))
        assert result["status"] == "failed"

        verify = Session()
        try:
            refreshed = verify.get(AdminJob, job_id)
            assert refreshed.status == AdminJobStatus.failed.value
            assert refreshed.error_message == "eroare simulata de optimizare"
        finally:
            verify.close()
    finally:
        _cleanup(engine, admin_job_ids=[job_id], org_id=org_id, user_id=user_id)
