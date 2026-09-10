from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.security import utcnow
from app.models.enums import OptimizationRunStatus
from app.models.forecast import ConsumptionForecast, PvForecast
from app.models.optimization import Plan, PlanInterval
from app.models.preference import PreferenceVersion
from app.models.tariff import Tariff, TariffVersion
from app.services.optimization_service import run_optimization_for_station
from tests.factories import make_org, make_station, make_user


def _add_tariffs(db, station, import_price="0.9", export_price="0.35"):
    for direction, price in (("import", import_price), ("export", export_price)):
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
    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(hours):
        t = start + timedelta(hours=h)
        hour = t.astimezone(timezone.utc).hour
        pv_kw = max(0.0, 4.0 * (1 - abs(hour - 13) / 7)) if 6 <= hour <= 20 else 0.0
        db.add(PvForecast(station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1), source="test", predicted_power_kw=Decimal(str(round(pv_kw, 3))), scenario="expected"))
    for q in range(hours * 4):
        t = start + timedelta(minutes=15 * q)
        load = 0.5 if t.astimezone(timezone.utc).hour < 6 else 1.2
        db.add(ConsumptionForecast(station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15), source="test", base_load_kw=Decimal(str(load)), ev_component_kw=Decimal("0"), flexible_component_kw=Decimal("0"), is_cold_start=True))
    db.flush()


def test_optimization_produces_balanced_feasible_plan(db):
    user = make_user(db, email="opt1@test.local")
    org = make_org(db, "Opt Org 1")
    station = make_station(db, org, user, name="Opt Station 1")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.status == OptimizationRunStatus.succeeded.value
    assert run.is_fallback is False

    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()
    assert len(intervals) > 0

    for pi in intervals:
        # Bilant energetic (aproximativ, tinand cont de randamente si rotunjiri):
        # pv + descarcare + import >= consum + incarcare + export - toleranta
        batt = float(pi.battery_power_target_kw)
        grid = float(pi.grid_power_target_kw)
        pv = float(pi.pv_forecast_kw)
        load = float(pi.load_forecast_kw)
        # grid = load + charge - pv - discharge  <=>  pv + grid + max(-batt,0) == load + max(batt,0) (aprox)
        lhs = pv + grid + max(-batt, 0)
        rhs = load + max(batt, 0)
        assert abs(lhs - rhs) < 0.05

        # Limite de putere respectate.
        assert -3.01 <= batt <= 3.01
        assert 0 <= float(pi.battery_soc_target_percent) <= 100.01

        # Nu se incarca din retea (allow_grid_charge=False implicit din factory).
        if batt > 0.01:
            assert batt <= pv + 0.05


def test_optimization_respects_no_grid_charge_and_no_battery_export(db):
    user = make_user(db, email="opt2@test.local")
    org = make_org(db, "Opt Org 2")
    station = make_station(db, org, user, name="Opt Station 2")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()
    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()

    for pi in intervals:
        batt = float(pi.battery_power_target_kw)
        pv = float(pi.pv_forecast_kw)
        load = float(pi.load_forecast_kw)
        if batt > 0.01:
            assert batt <= pv + 0.05, "bateria nu ar trebui sa se incarce din retea"
        if batt < -0.01:
            assert abs(batt) <= load + 0.05, "bateria nu ar trebui sa exporte in retea"


def test_optimization_infeasible_constraints_trigger_fallback(db):
    user = make_user(db, email="opt3@test.local")
    org = make_org(db, "Opt Org 3")
    station = make_station(db, org, user, name="Opt Station 3")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    db.commit()

    # Obiectiv imposibil: SOC minim > SOC maxim normal.
    pref = db.scalar(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id))
    pref.min_reserve_soc_percent = Decimal("90")
    pref.max_normal_soc_percent = Decimal("20")
    db.add(pref)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.is_fallback is True
    assert run.status == OptimizationRunStatus.infeasible.value

    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()
    assert all(float(pi.battery_power_target_kw) == 0 for pi in intervals), "planul de fallback trebuie sa fie de asteptare (baterie in hold)"


def test_optimization_fallback_when_no_forecasts_available(db):
    user = make_user(db, email="opt4@test.local")
    org = make_org(db, "Opt Org 4")
    station = make_station(db, org, user, name="Opt Station 4")
    _add_tariffs(db, station)
    db.commit()  # fara prognoze PV/consum

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.is_fallback is True
    assert "Prognoze" in run.fallback_reason or "prognoz" in run.fallback_reason.lower()
