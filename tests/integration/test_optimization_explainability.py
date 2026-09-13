"""Teste HTTP pentru explicabilitatea planurilor de optimizare si pasul de
confirmare la reexecutare (issue #47):
  - `GET /admin/operations/optimize-confirm` (rezumatul inputurilor inainte
    de a inlocui un plan activ);
  - `POST /admin/operations/optimize/{station_id}` -- confirmare ceruta
    STRICT cand exista deja un plan activ pentru statie;
  - `GET /admin/operations/optimization-runs/{run_id}` (tabelul explicabil,
    legenda, segmentele in limbaj natural si provenance/freshness).

Ca si `test_admin_operations_routes.py`, `.delay()` e inlocuit cu un stub --
nu lasam taskul Celery real sa ruleze. Rularea de optimizare propriu-zisa
(pentru a avea un `Plan` activ real de aratat) se face printr-un apel Python
direct catre `run_optimization_for_station`, nu prin worker."""
from __future__ import annotations

import uuid
from datetime import UTC, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.models.admin_job import AdminJob
from app.models.enums import OptimizationRunStatus, PlanStatus
from app.models.optimization import OptimizationRun, Plan
from app.models.tariff import Tariff, TariffVersion
from app.services.optimization_service import run_optimization_for_station
from app.workers import tasks as tasks_module
from tests.factories import make_org, make_station, make_user
from tests.web_helpers import login


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    reset_key("login_attempts:testclient")


class _FakeAsyncResult:
    def __init__(self, task_id: str):
        self.id = task_id


def _stub_delay(monkeypatch):
    calls: list = []

    def _fake_delay(*args, **kwargs):
        calls.append(args)
        return _FakeAsyncResult("fake-task-id")

    monkeypatch.setattr(tasks_module.admin_optimize_station_job_task, "delay", _fake_delay)
    return calls


def _add_tariffs(db, station):
    for direction, price in (("import", "0.9"), ("export", "0.35")):
        t = Tariff(station_id=station.id, direction=direction, kind="fixed", name=f"t-{direction}")
        db.add(t)
        db.flush()
        db.add(
            TariffVersion(
                tariff_id=t.id, valid_from=utcnow() - timedelta(days=1),
                fixed_price_lei_per_kwh=Decimal(price), fixed_monthly_fee_lei=Decimal("0"),
                variable_component_lei_per_kwh=Decimal("0"),
            )
        )
    db.flush()


def _add_forecasts(db, station, hours=40):
    from app.models.forecast import ConsumptionForecast, PvForecast

    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(hours):
        t = start + timedelta(hours=h)
        hour = t.astimezone(UTC).hour
        pv_kw = max(0.0, 4.0 * (1 - abs(hour - 13) / 7)) if 6 <= hour <= 20 else 0.0
        db.add(
            PvForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                source="test", predicted_power_kw=Decimal(str(round(pv_kw, 3))), scenario="expected",
            )
        )
    for q in range(hours * 4):
        t = start + timedelta(minutes=15 * q)
        load = 0.5 if t.astimezone(UTC).hour < 6 else 1.2
        db.add(
            ConsumptionForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                source="test", base_load_kw=Decimal(str(load)), ev_component_kw=Decimal("0"),
                flexible_component_kw=Decimal("0"), is_cold_start=True,
            )
        )
    db.flush()


def _block_weather_refresh(monkeypatch):
    from app.services import weather_service
    from app.services.weather_service import WeatherUnavailableError

    def _always_unavailable(*args, **kwargs):
        raise WeatherUnavailableError("blocat explicit pentru test")

    monkeypatch.setattr(weather_service, "refresh_weather_for_station", _always_unavailable)


def _make_station_with_real_plan(db, monkeypatch, *, org_name, station_name, admin_email):
    """Ruleaza optimizarea reala (solver inclus) pentru a produce un `Plan`
    publicat, activ, cu date reale de explicat -- nu un fixture inventat."""
    admin = make_user(db, email=admin_email, password="Password1234", is_platform_admin=True)
    org = make_org(db, org_name)
    station = make_station(db, org, admin, name=station_name)
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    _block_weather_refresh(monkeypatch)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()
    return station, run


def test_optimization_confirm_page_shows_no_active_plan_for_fresh_station(client, db):
    admin = make_user(db, email="optux1@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "OptUX Org 1")
    station = make_station(db, org, admin, name="OptUX Station 1")
    db.commit()

    login(client, "optux1@test.local", "Password1234")

    resp = client.get(f"/admin/operations/optimize-confirm?station_id={station.id}")
    assert resp.status_code == 200
    assert "nu are niciun plan activ" in resp.text
    assert station.name in resp.text


def test_optimization_confirm_page_warns_about_active_plan(client, db, monkeypatch):
    station, run = _make_station_with_real_plan(
        db, monkeypatch, org_name="OptUX Org 2", station_name="OptUX Station 2", admin_email="optux2@test.local"
    )
    assert run.status == OptimizationRunStatus.succeeded.value

    login(client, "optux2@test.local", "Password1234")
    resp = client.get(f"/admin/operations/optimize-confirm?station_id={station.id}")

    assert resp.status_code == 200
    assert "va fi inlocuit" in resp.text
    assert "v1" in resp.text


def test_optimization_confirm_page_unknown_station_redirects(client, db):
    make_user(db, email="optux3@test.local", password="Password1234", is_platform_admin=True)
    db.commit()
    login(client, "optux3@test.local", "Password1234")

    resp = client.get(f"/admin/operations/optimize-confirm?station_id={uuid.uuid4()}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations?error=station_not_found"


def test_trigger_optimization_requires_confirmation_when_active_plan_exists(client, db, monkeypatch):
    station, _run = _make_station_with_real_plan(
        db, monkeypatch, org_name="OptUX Org 4", station_name="OptUX Station 4", admin_email="optux4@test.local"
    )
    calls = _stub_delay(monkeypatch)

    login(client, "optux4@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    jobs_before = db.scalars(select(AdminJob).where(AdminJob.station_id == station.id)).all()

    resp = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations?error=optimization_confirmation_required"
    assert len(calls) == 0
    jobs_after = db.scalars(select(AdminJob).where(AdminJob.station_id == station.id)).all()
    assert len(jobs_after) == len(jobs_before), "niciun AdminJob nu trebuie creat fara confirmare explicita"


def test_trigger_optimization_succeeds_with_confirmation_and_records_reason(client, db, monkeypatch):
    station, _run = _make_station_with_real_plan(
        db, monkeypatch, org_name="OptUX Org 5", station_name="OptUX Station 5", admin_email="optux5@test.local"
    )
    calls = _stub_delay(monkeypatch)

    login(client, "optux5@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(
        f"/admin/operations/optimize/{station.id}",
        data={"csrf_token": csrf, "confirmed": "true", "reason": "pret actualizat manual"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations"
    assert len(calls) == 1

    job = db.scalar(select(AdminJob).where(AdminJob.station_id == station.id).order_by(AdminJob.created_at.desc()))
    assert job is not None
    assert job.params["reason"] == "pret actualizat manual"


def test_trigger_optimization_without_active_plan_does_not_require_confirmation(client, db, monkeypatch):
    """Regresie: o statie fara plan activ (prima rulare) trebuie sa functioneze
    fara nicio confirmare -- nu are nimic de inlocuit."""
    admin = make_user(db, email="optux6@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "OptUX Org 6")
    station = make_station(db, org, admin, name="OptUX Station 6")
    db.commit()

    calls = _stub_delay(monkeypatch)
    login(client, "optux6@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations"
    assert len(calls) == 1


def test_optimization_run_detail_renders_explainable_table_and_legend(client, db, monkeypatch):
    _station, run = _make_station_with_real_plan(
        db, monkeypatch, org_name="OptUX Org 7", station_name="OptUX Station 7", admin_email="optux7@test.local"
    )

    login(client, "optux7@test.local", "Password1234")
    resp = client.get(f"/admin/operations/optimization-runs/{run.id}")

    assert resp.status_code == 200
    body = resp.text
    # Legenda coloanelor
    assert "+ incarcare, - descarcare" in body
    assert "+ import, - export" in body
    # Provenance/freshness
    assert "Provenance" in body
    assert "SOC baterie folosit ca punct de start" in body
    # Rezumat in limbaj natural pe segmente
    assert "Rezumat in limbaj natural" in body


def test_optimization_run_detail_explains_fallback_reason_when_no_config(client, db):
    admin = make_user(db, email="optux8@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "OptUX Org 8")
    station = make_station(db, org, admin, name="OptUX Station 8")
    db.commit()

    # Sterge configuratia/preferintele publicate ca sa forteze fallback-ul
    # "statia nu are configuratie sau preferinte publicate".
    from app.models.preference import PreferenceVersion
    from app.models.station import StationConfigVersion

    for row in db.scalars(select(StationConfigVersion).where(StationConfigVersion.station_id == station.id)).all():
        db.delete(row)
    for row in db.scalars(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id)).all():
        db.delete(row)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()
    assert run.is_fallback is True

    login(client, "optux8@test.local", "Password1234")
    resp = client.get(f"/admin/operations/optimization-runs/{run.id}")

    assert resp.status_code == 200
    assert "De ce nu exista un plan optimizat live" in resp.text
    assert "nu are configuratie sau preferinte publicate" in resp.text
    # Fara input_snapshot -> mesajul explicit de date insuficiente, nu o eroare tacuta.
    assert "Nu exista un snapshot de intrare" in resp.text


def test_optimization_run_detail_unknown_run_redirects(client, db):
    make_user(db, email="optux9@test.local", password="Password1234", is_platform_admin=True)
    db.commit()
    login(client, "optux9@test.local", "Password1234")

    resp = client.get(f"/admin/operations/optimization-runs/{uuid.uuid4()}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/operations?error=optimization_run_not_found"


def test_operations_page_links_to_run_detail(client, db, monkeypatch):
    _station, run = _make_station_with_real_plan(
        db, monkeypatch, org_name="OptUX Org 10", station_name="OptUX Station 10", admin_email="optux10@test.local"
    )
    login(client, "optux10@test.local", "Password1234")

    resp = client.get("/admin/operations")
    assert resp.status_code == 200
    assert f"/admin/operations/optimization-runs/{run.id}" in resp.text


def test_trigger_optimization_rejects_duplicate_still_works_after_confirmation_change(client, db, monkeypatch):
    """Regresie directa a testului existent din `test_admin_operations_routes.py`
    (fara plan activ, deci fara nevoie de `confirmed`) -- garanteaza ca noua
    verificare de confirmare nu a rupt protectia la declansare duplicata."""
    admin = make_user(db, email="optux11@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "OptUX Org 11")
    station = make_station(db, org, admin, name="OptUX Station 11")
    db.commit()

    _stub_delay(monkeypatch)
    login(client, "optux11@test.local", "Password1234")
    csrf = client.cookies.get("ems_csrf")

    resp1 = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp1.status_code == 303

    resp2 = client.post(f"/admin/operations/optimize/{station.id}", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/admin/operations?error=optimization_in_progress"


def test_plan_status_constant_matches_publish_plan_filter():
    """Documenteaza dependenta intre `admin._ACTIVE_PLAN_STATUSES` si filtrul
    din `optimization_service._publish_plan` -- daca cineva schimba unul fara
    celalalt, planul "activ" din UI ar deveni gresit."""
    from app.web.routes.admin import _ACTIVE_PLAN_STATUSES

    assert set(_ACTIVE_PLAN_STATUSES) == {
        PlanStatus.published.value, PlanStatus.accepted_by_device.value, PlanStatus.executing.value,
    }


def test_active_plan_lookup_ignores_superseded_plans(db):
    from app.web.routes.admin import _active_plan_for_station

    admin = make_user(db, email="optux12@test.local", password="Password1234")
    org = make_org(db, "OptUX Org 12")
    station = make_station(db, org, admin, name="OptUX Station 12")
    db.commit()

    run = OptimizationRun(
        station_id=station.id, status=OptimizationRunStatus.succeeded.value,
        horizon_start=utcnow(), horizon_end=utcnow() + timedelta(hours=1), interval_minutes=15,
        triggered_by="user",
    )
    db.add(run)
    db.flush()
    plan = Plan(
        optimization_run_id=run.id, station_id=station.id, version=1,
        status=PlanStatus.superseded.value, execution_mode="shadow",
    )
    db.add(plan)
    db.commit()

    assert _active_plan_for_station(db, station.id) is None
