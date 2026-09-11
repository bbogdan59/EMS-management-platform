from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.security import utcnow
from app.models.forecast import ConsumptionForecast, PvForecast
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryAggregate
from app.services import dashboard_service as dashboard
from tests.factories import make_org, make_station, make_user


def _add_tariff_version(db, station, direction, *, valid_from, valid_to=None, fixed_price):
    tariff = db.query(Tariff).filter_by(station_id=station.id, direction=direction).one_or_none()
    if tariff is None:
        tariff = Tariff(station_id=station.id, direction=direction, kind="fixed", name=f"t-{direction}")
        db.add(tariff)
        db.flush()
    db.add(
        TariffVersion(
            tariff_id=tariff.id, valid_from=valid_from, valid_to=valid_to,
            fixed_price_lei_per_kwh=Decimal(str(fixed_price)), opcom_margin_lei_per_kwh=None,
            fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        )
    )
    db.flush()
    return tariff


def _add_hour(db, station, period_start, *, load=None, pv=None, grid_import=None, grid_export=None):
    db.add(
        TelemetryAggregate(
            station_id=station.id, period_type="hour", period_start=period_start, period_end=period_start + timedelta(hours=1),
            load_energy_kwh=Decimal(str(load)) if load is not None else None,
            pv_energy_kwh=Decimal(str(pv)) if pv is not None else None,
            grid_import_energy_kwh=Decimal(str(grid_import)) if grid_import is not None else None,
            grid_export_energy_kwh=Decimal(str(grid_export)) if grid_export is not None else None,
            coverage={"load": 1.0, "pv": 1.0, "grid": 1.0},
        )
    )
    db.flush()


def _station(db, suffix=""):
    user = make_user(db, email=f"dash{suffix}@test.local")
    org = make_org(db, f"Dash Org {suffix}")
    return make_station(db, org, user, name=f"Dash Station {suffix}")


# --- get_estimated_savings ---------------------------------------------


def test_estimated_savings_uses_historical_tariff_not_a_blanket_current_price(db):
    """Bug corectat: calculul folosea tariful CURENT (la momentul apelului)
    aplicat retroactiv intregului interval cerut. Doua versiuni de tarif cu
    preturi foarte diferite, fiecare valabila DOAR pentru o ora din fereastra
    ceruta -- suma trebuie sa reflecte fiecare pret in ora lui, nu unul singur."""
    station = _station(db, "hist")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)

    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=365), valid_to=t0 + timedelta(hours=1), fixed_price="1.0")
    _add_tariff_version(db, station, "import", valid_from=t0 + timedelta(hours=1), fixed_price="5.0")

    _add_hour(db, station, t0, load=10, pv=0, grid_import=10, grid_export=0)
    _add_hour(db, station, t0 + timedelta(hours=1), load=10, pv=0, grid_import=10, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=2))

    assert result["available"] is True
    assert result["hours_priced"] == 2
    # 10 kWh * 1.0 lei/kWh (ora 0) + 10 kWh * 5.0 lei/kWh (ora 1) = 60 lei --
    # nu 20 (doar pretul vechi) si nu 100 (doar pretul nou aplicat gresit peste tot).
    assert abs(result["actual_net_cost_lei"] - 60.0) < 0.01


def test_estimated_savings_separates_whole_system_and_ems_incremental_benefit(db):
    """Beneficiul intregului sistem PV/baterie (fata de "fara nimic") trebuie
    sa fie distinct de beneficiul INCREMENTAL al EMS (fata de un auto-consum
    simplu, fara arbitraj activ al bateriei) -- numere verificate manual."""
    station = _station(db, "split")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)

    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_tariff_version(db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.4")

    # O ora: consum 5 kWh, PV 2 kWh (acopera partial), bateria descarca restul
    # de 2 kWh -- doar 1 kWh ramane de importat din retea.
    _add_hour(db, station, t0, load=5, pv=2, grid_import=1, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    # Fara PV/baterie: tot consumul (5 kWh) importat la 1.0 lei/kWh = 5 lei.
    assert abs(result["whole_system_baseline_cost_lei"] - 5.0) < 0.01
    # Real: doar 1 kWh importat = 1 lei.
    assert abs(result["actual_net_cost_lei"] - 1.0) < 0.01
    # Beneficiu sistem complet = 5 - 1 = 4 lei (2 kWh PV + 2 kWh baterie, ambele evitate la import).
    assert abs(result["whole_system_benefit_lei"] - 4.0) < 0.01
    # Auto-consum simplu (fara baterie): deficit = max(5-2,0) = 3 kWh importate = 3 lei.
    assert abs(result["ems_incremental_baseline_cost_lei"] - 3.0) < 0.01
    # Beneficiu incremental EMS = 3 - 1 = 2 lei (doar contributia bateriei, nu si a PV-ului).
    assert abs(result["ems_incremental_benefit_lei"] - 2.0) < 0.01


def test_estimated_savings_export_revenue_reduces_net_cost(db):
    station = _station(db, "export")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_tariff_version(db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.4")

    # Surplus PV exportat -- venitul de export trebuie sa scada costul net (poate deveni negativ).
    _add_hour(db, station, t0, load=5, pv=8, grid_import=0, grid_export=3)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    # 0 kWh import - 3 kWh export * 0.4 lei/kWh = -1.2 lei (venit net, nu cost).
    assert abs(result["actual_net_cost_lei"] - (-1.2)) < 0.01


def test_estimated_savings_excludes_hours_without_resolvable_price_and_reports_coverage(db):
    station = _station(db, "coverage")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    # Tariful expira dupa prima ora -- a doua ora ramane fara pret rezolvabil.
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), valid_to=t0 + timedelta(hours=1), fixed_price="1.0")

    _add_hour(db, station, t0, load=10, pv=0, grid_import=10, grid_export=0)
    _add_hour(db, station, t0 + timedelta(hours=1), load=10, pv=0, grid_import=10, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=2))

    assert result["available"] is True
    assert result["hours_expected"] == 2
    assert result["hours_priced"] == 1
    assert result["hours_with_load_but_no_price"] == 1
    assert abs(result["coverage_ratio"] - 0.5) < 0.001
    # Doar ora cu pret rezolvabil intra in suma -- 10 kWh, nu 20.
    assert abs(result["total_load_kwh"] - 10.0) < 0.01


def test_estimated_savings_unavailable_without_any_tariff(db):
    station = _station(db, "notariff")
    t0 = utcnow() - timedelta(days=10)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is False
    assert "reason" in result


# --- get_forecast_vs_actual ---------------------------------------------


def test_forecast_vs_actual_ignores_forecast_issued_after_the_predicted_interval(db):
    """O prognoza regenerata ULTERIOR (issued_at dupa interval_start) nu
    trebuie folosita pentru compararea istorica -- ar insemna folosirea
    retroactiva a unei informatii care inca nu exista la acel moment."""
    station = _station(db, "asof1")
    t = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)

    db.add(PvForecast(
        station_id=station.id, issued_at=t - timedelta(hours=2), interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", predicted_power_kw=Decimal("3.0"), scenario="expected",
    ))
    db.add(PvForecast(
        station_id=station.id, issued_at=t + timedelta(hours=1), interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", predicted_power_kw=Decimal("99.0"), scenario="expected",  # "din viitor" fata de t
    ))
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "pv", t - timedelta(hours=1), t + timedelta(hours=1))

    assert len(out) == 1  # nicio dublare de punct pentru acelasi interval_start
    assert abs(out[0]["forecast_kw"] - 3.0) < 0.001


def test_forecast_vs_actual_picks_most_recent_valid_as_of_forecast(db):
    station = _station(db, "asof2")
    t = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)

    db.add(PvForecast(
        station_id=station.id, issued_at=t - timedelta(hours=3), interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", predicted_power_kw=Decimal("1.0"), scenario="expected",
    ))
    db.add(PvForecast(
        station_id=station.id, issued_at=t - timedelta(hours=1), interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", predicted_power_kw=Decimal("2.5"), scenario="expected",  # mai recenta, tot inainte de t
    ))
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "pv", t - timedelta(hours=1), t + timedelta(hours=1))

    assert len(out) == 1
    assert abs(out[0]["forecast_kw"] - 2.5) < 0.001


def test_forecast_vs_actual_load_metric_uses_consumption_forecast(db):
    station = _station(db, "asof3")
    t = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)

    db.add(ConsumptionForecast(
        station_id=station.id, issued_at=t - timedelta(hours=1), interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", base_load_kw=Decimal("0.5"), ev_component_kw=Decimal("0.2"), flexible_component_kw=Decimal("0.1"),
        is_cold_start=False,
    ))
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "load", t - timedelta(hours=1), t + timedelta(hours=1))

    assert len(out) == 1
    assert abs(out[0]["forecast_kw"] - 0.8) < 0.001


# --- get_plan_chart -------------------------------------------------------


def _make_plan(db, station):
    run = OptimizationRun(
        station_id=station.id, status="succeeded",
        horizon_start=utcnow(), horizon_end=utcnow() + timedelta(hours=1),
    )
    db.add(run)
    db.flush()
    plan = Plan(optimization_run_id=run.id, station_id=station.id, version=1, status="published", execution_mode="shadow")
    db.add(plan)
    db.flush()
    return plan


def test_plan_chart_exposes_observed_fields_distinctly_from_planned(db):
    station = _station(db, "plan")
    plan = _make_plan(db, station)
    t = utcnow().replace(minute=0, second=0, microsecond=0)

    db.add(PlanInterval(
        plan_id=plan.id, interval_start=t, interval_end=t + timedelta(minutes=15),
        pv_forecast_kw=Decimal("1.0"), load_forecast_kw=Decimal("0.5"),
        battery_power_target_kw=Decimal("2.0"), grid_power_target_kw=Decimal("0.0"),
        battery_soc_target_percent=Decimal("60.0"),
        observed_battery_power_kw=Decimal("1.8"), observed_grid_power_kw=Decimal("0.1"),
        observed_soc_percent=Decimal("59.0"), deviation_notes="usor sub tinta",
    ))
    db.add(PlanInterval(
        plan_id=plan.id, interval_start=t + timedelta(minutes=15), interval_end=t + timedelta(minutes=30),
        pv_forecast_kw=Decimal("1.0"), load_forecast_kw=Decimal("0.5"),
        battery_power_target_kw=Decimal("2.0"), grid_power_target_kw=Decimal("0.0"),
        battery_soc_target_percent=Decimal("62.0"),
        # niciun observed_* -- inca neconciliat (nicio valoare fabricata).
    ))
    db.commit()

    result = dashboard.get_plan_chart(db, station)

    assert result["plan"]["status"] == "published"
    first, second = result["intervals"]
    assert abs(first["observed_battery_kw"] - 1.8) < 0.001
    assert abs(first["observed_grid_kw"] - 0.1) < 0.001
    assert abs(first["observed_soc_pct"] - 59.0) < 0.001
    assert first["deviation_notes"] == "usor sub tinta"
    assert second["observed_battery_kw"] is None
    assert second["observed_grid_kw"] is None
    assert second["observed_soc_pct"] is None
    assert second["deviation_notes"] is None
