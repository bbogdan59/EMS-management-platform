from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.security import utcnow
from app.models.forecast import ConsumptionForecast, PvForecast, WeatherForecast
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services import dashboard_service as dashboard
from tests.factories import make_device, make_market_day, make_org, make_station, make_user


def _add_tariff_version(
    db, station, direction, *, valid_from, valid_to=None, fixed_price,
    variable_component=0, distribution=0, transport=0, other_regulated=0, vat_rate=None,
):
    tariff = db.query(Tariff).filter_by(station_id=station.id, direction=direction).one_or_none()
    if tariff is None:
        tariff = Tariff(station_id=station.id, direction=direction, kind="fixed", name=f"t-{direction}")
        db.add(tariff)
        db.flush()
    db.add(
        TariffVersion(
            tariff_id=tariff.id, valid_from=valid_from, valid_to=valid_to,
            fixed_price_lei_per_kwh=Decimal(str(fixed_price)), opcom_margin_lei_per_kwh=None,
            fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal(str(variable_component)),
            distribution_lei_per_kwh=Decimal(str(distribution)), transport_lei_per_kwh=Decimal(str(transport)),
            other_regulated_lei_per_kwh=Decimal(str(other_regulated)),
            vat_rate_percent=Decimal(str(vat_rate)) if vat_rate is not None else None,
        )
    )
    db.flush()
    return tariff


def _add_indexed_tariff_version(db, station, direction, *, valid_from, valid_to=None, margin):
    tariff = Tariff(station_id=station.id, direction=direction, kind="indexed_opcom", name=f"idx-{direction}")
    db.add(tariff)
    db.flush()
    db.add(
        TariffVersion(
            tariff_id=tariff.id, valid_from=valid_from, valid_to=valid_to,
            fixed_price_lei_per_kwh=None, opcom_margin_lei_per_kwh=Decimal(str(margin)),
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


def _add_15m_load(db, station, period_start, *, load_kwh, coverage=1.0):
    db.add(
        TelemetryAggregate(
            station_id=station.id,
            period_type="interval_15m",
            period_start=period_start,
            period_end=period_start + timedelta(minutes=15),
            load_energy_kwh=Decimal(str(load_kwh)) if load_kwh is not None else None,
            coverage={"load": coverage},
        )
    )
    db.flush()


def _station(db, suffix="", **overrides):
    user = make_user(db, email=f"dash{suffix}@test.local")
    org = make_org(db, f"Dash Org {suffix}")
    return make_station(db, org, user, name=f"Dash Station {suffix}", **overrides)


def _add_raw(db, station, device, measured_at, *, pv=None, load=None, battery=None, grid=None, soc=None, sequence=0, is_simulated=False, is_late=False):
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="boot-1", sequence=sequence,
            measured_at=measured_at, received_at=measured_at,
            pv_power_w=Decimal(str(pv * 1000)) if pv is not None else None,
            load_power_w=Decimal(str(load * 1000)) if load is not None else None,
            battery_power_w=Decimal(str(battery * 1000)) if battery is not None else None,
            grid_power_w=Decimal(str(grid * 1000)) if grid is not None else None,
            battery_soc_percent=Decimal(str(soc)) if soc is not None else None,
            is_simulated=is_simulated, is_late=is_late,
        )
    )
    db.flush()


def _add_chart_aggregate(db, station, period_type, start, end, *, pv_kwh, soc=50, data_quality="measured"):
    db.add(
        TelemetryAggregate(
            station_id=station.id,
            period_type=period_type,
            period_start=start,
            period_end=end,
            pv_energy_kwh=Decimal(str(pv_kwh)),
            load_energy_kwh=Decimal("0"),
            battery_charge_energy_kwh=Decimal("0"),
            battery_discharge_energy_kwh=Decimal("0"),
            grid_import_energy_kwh=Decimal("0"),
            grid_export_energy_kwh=Decimal("0"),
            avg_battery_soc_percent=Decimal(str(soc)),
            sample_count=1,
            data_quality=data_quality,
            coverage={"pv": 1.0, "load": 1.0, "battery": 1.0, "grid": 1.0, "soc": 1.0},
        )
    )
    db.flush()


# --- get_timeseries_chart (issue #33: rezolutie server-side + agregare metric-aware) --


def test_timeseries_chart_declares_resolution_aggregation_timezone_and_coverage(db):
    station = _station(db, "ts1")
    device = make_device(db, station)
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _add_raw(db, station, device, t0, pv=1.0, load=2.0, battery=0.5, grid=1.5, soc=50.0, sequence=1)
    db.commit()

    result = dashboard.get_timeseries_chart(db, station, t0 - timedelta(minutes=5), t0 + timedelta(minutes=5), "24h")

    assert result["resolution"] == "15m"
    assert result["aggregation"]["pv_kw"] == "mean"
    assert result["aggregation"]["soc_pct"] == "mean"
    assert result["timezone"] == station.timezone
    assert 0.0 <= result["coverage"] <= 1.0
    assert len(result["points"]) == 1


def test_timeseries_chart_averages_power_within_a_bucket_not_sums(db):
    """Doua esantioane brute in acelasi bucket de 15 min -- puterea trebuie
    mediata, nu insumata (ar dubla artificial valoarea afisata)."""
    station = _station(db, "ts2")
    device = make_device(db, station)
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _add_raw(db, station, device, t0, pv=2.0, sequence=1)
    _add_raw(db, station, device, t0 + timedelta(minutes=5), pv=4.0, sequence=2)
    db.commit()

    result = dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(minutes=10), "24h")

    assert len(result["points"]) == 1
    assert abs(result["points"][0]["pv_kw"] - 3.0) < 0.001  # medie (2+4)/2, nu suma (6)


def test_timeseries_chart_soc_is_averaged_never_summed_across_bucket(db):
    station = _station(db, "ts3")
    device = make_device(db, station)
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _add_raw(db, station, device, t0, soc=40.0, sequence=1)
    _add_raw(db, station, device, t0 + timedelta(minutes=5), soc=60.0, sequence=2)
    db.commit()

    result = dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(minutes=10), "24h")

    # Medie (40+60)/2 = 50 -- daca ar fi insumat gresit ar iesi 100 (peste 100%, imposibil fizic).
    assert abs(result["points"][0]["soc_pct"] - 50.0) < 0.001


def test_timeseries_chart_uses_coarser_resolution_for_longer_ranges(db):
    station = _station(db, "ts4")
    device = make_device(db, station)
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    _add_raw(db, station, device, t0, pv=1.0, sequence=1)
    db.commit()

    assert dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(hours=1), "24h")["resolution"] == "15m"
    assert dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(hours=1), "7d")["resolution"] == "30m"
    assert dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(hours=1), "30d")["resolution"] == "1h"
    assert dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(hours=1), "1y")["resolution"] == "1d"


def test_one_year_chart_uses_local_calendar_day_rollup_including_dst_duration(db):
    station = _station(db, "ts-local-day")
    tz = ZoneInfo(station.timezone)
    start = datetime(2026, 3, 29, 0, tzinfo=tz).astimezone(UTC)
    end = datetime(2026, 3, 30, 0, tzinfo=tz).astimezone(UTC)
    assert end - start == timedelta(hours=23)
    _add_chart_aggregate(db, station, "day", start, end, pv_kwh=23)
    db.commit()

    result = dashboard.get_timeseries_chart(db, station, start, end, "1y")

    assert len(result["points"]) == 1
    assert result["points"][0]["t"] == start.isoformat()
    assert result["points"][0]["pv_kw"] == 1.0


def test_timeseries_chart_missing_data_is_not_a_zero_filled_series(db):
    """Fara nicio telemetrie in interval -- raspunsul trebuie sa aiba `points`
    goale (ca UI-ul sa arate un empty-state gri), NU o serie umpluta cu 0."""
    station = _station(db, "ts5")
    make_device(db, station)
    db.commit()
    t0 = utcnow() - timedelta(hours=2)

    result = dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(hours=1), "24h")

    assert result["points"] == []
    assert result["coverage"] == 0.0


def test_timeseries_chart_exposes_bucket_quality_for_raw_telemetry(db):
    station = _station(db, "ts-quality-raw")
    device = make_device(db, station)
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _add_raw(db, station, device, t0, pv=1.0, sequence=1, is_simulated=True)
    _add_raw(db, station, device, t0 + timedelta(minutes=15), pv=2.0, sequence=2, is_late=True)
    db.commit()

    result = dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(minutes=30), "24h")

    assert [point["data_quality"] for point in result["points"]] == ["simulated", "stale"]


def test_timeseries_chart_exposes_bucket_quality_for_persisted_rollups(db):
    station = _station(db, "ts-quality-rollup")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)
    _add_chart_aggregate(db, station, "interval_15m", t0, t0 + timedelta(minutes=15), pv_kwh=1, data_quality="estimated")
    _add_chart_aggregate(
        db, station, "interval_15m", t0 + timedelta(minutes=15), t0 + timedelta(minutes=30), pv_kwh=1, data_quality="simulated"
    )
    db.commit()

    result = dashboard.get_timeseries_chart(db, station, t0, t0 + timedelta(minutes=30), "7d")

    assert result["points"][0]["data_quality"] == "simulated"


def test_timeseries_raw_export_still_returns_flat_list_unaggregated(db):
    """Exportul CSV foloseste `get_timeseries_raw`, care trebuie sa ramana
    neschimbat (fara agregare) -- contractul vechi al exportului."""
    station = _station(db, "ts6")
    device = make_device(db, station)
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _add_raw(db, station, device, t0, pv=1.0, sequence=1)
    _add_raw(db, station, device, t0 + timedelta(minutes=5), pv=2.0, sequence=2)
    db.commit()

    rows = dashboard.get_timeseries_raw(db, station, t0, t0 + timedelta(minutes=10))

    assert len(rows) == 2  # neagregat -- ambele randuri brute raman distincte
    assert isinstance(rows[0]["t"], str)  # isoformat, cum astepta deja exportul CSV


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


def test_estimated_savings_export_revenue_ignores_export_grid_charges(db):
    station = _station(db, "exportcharges")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_tariff_version(
        db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.4",
        distribution="0.2", transport="0.05", other_regulated="0.03", vat_rate="19",
    )

    _add_hour(db, station, t0, load=0, pv=3, grid_import=0, grid_export=3)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    assert result["export_revenue_lei"] == 1.43
    assert result["actual_net_cost_lei"] == -1.43


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


# --- get_estimated_savings: detaliere financiara (issue #49) -----------


def test_estimated_savings_financial_breakdown_zero_production(db):
    """Zero productie PV: valoarea bruta PV si economia de autoconsum trebuie
    sa fie 0 -- fara PV nu exista nimic de valorificat, indiferent de pret."""
    station = _station(db, "zeropv")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_tariff_version(db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.4")

    _add_hour(db, station, t0, load=10, pv=0, grid_import=10, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    assert result["gross_pv_value_lei"] == 0.0
    assert result["self_consumption_savings_lei"] == 0.0
    assert result["export_revenue_lei"] == 0.0


def test_estimated_savings_financial_breakdown_zero_consumption(db):
    """Zero consum, tot PV-ul exportat: economia de autoconsum e 0 (nimic
    consumat pe loc), iar valoarea bruta PV = venitul de export (tot PV-ul
    ajunge in retea, la pretul de export, nu de import -- numere distincte)."""
    station = _station(db, "zeroload")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_tariff_version(db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.4")

    _add_hour(db, station, t0, load=0, pv=6, grid_import=0, grid_export=6)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    assert result["self_consumption_savings_lei"] == 0.0
    # Valoare bruta PV = 6 kWh * 1.0 lei/kWh (pret de CUMPARARE, cost evitat potential) = 6 lei.
    assert abs(result["gross_pv_value_lei"] - 6.0) < 0.01
    # Venit export = 6 kWh * 0.4 lei/kWh (pret de VANZARE) = 2.4 lei -- deliberat diferit
    # de valoarea bruta PV, tocmai ca sa nu fie confundate (definitia din issue #49).
    assert abs(result["export_revenue_lei"] - 2.4) < 0.01


def test_estimated_savings_financial_breakdown_self_consumption_and_export_split(db):
    """Caz mixt: o parte din PV e autoconsumata, restul exportata -- cele doua
    numere trebuie sa se separe corect, iar valoarea bruta PV sa fie suma lor
    "echivalenta in lei de cumparare" doar cand pretul de cumparare == vanzare."""
    station = _station(db, "mixedpv")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_tariff_version(db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.4")

    # 8 kWh PV, 5 kWh consum -> 5 kWh autoconsumate, 3 kWh exportate (fara baterie).
    _add_hour(db, station, t0, load=5, pv=8, grid_import=0, grid_export=3)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    assert abs(result["gross_pv_value_lei"] - 8.0) < 0.01  # 8 kWh * 1.0 lei/kWh
    assert abs(result["self_consumption_savings_lei"] - 5.0) < 0.01  # 5 kWh * 1.0 lei/kWh
    assert abs(result["export_revenue_lei"] - 1.2) < 0.01  # 3 kWh * 0.4 lei/kWh


def test_estimated_savings_financial_breakdown_negative_price_interval(db):
    """Interval de pret negativ (posibil pe OPCOM): valoarea bruta PV devine
    NEGATIVA -- corect matematic (a produce energie cand pretul e negativ nu
    are valoare de cumparare evitata, ci una negativa), nu trunchiat la 0."""
    station = _station(db, "negprice")
    day = date(2020, 6, 1)
    t0 = datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
    make_market_day(db, day, [-50.0])  # -50 lei/MWh = -0.05 lei/kWh, un singur interval pe toata ziua.
    _add_indexed_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), margin=0.0)
    _add_tariff_version(db, station, "export", valid_from=t0 - timedelta(days=1), fixed_price="0.0")

    _add_hour(db, station, t0, load=10, pv=4, grid_import=6, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["available"] is True
    # 4 kWh PV * (-0.05 lei/kWh) = -0.2 lei.
    assert abs(result["gross_pv_value_lei"] - (-0.2)) < 0.001


def test_estimated_savings_tariff_provenance_fixed_contract(db):
    station = _station(db, "provfixed")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_hour(db, station, t0, load=10, pv=0, grid_import=10, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["tariff_buy_provenance"] == {"fixed_contract": 1, "indexed_settled": 0, "indexed_synthetic": 0, "unknown": 0}
    assert result["tariff_provenance_summary"] == "measured"


def test_estimated_savings_tariff_provenance_indexed_settled_real_market_price(db):
    station = _station(db, "provsettled")
    day = date(2020, 6, 2)
    t0 = datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
    make_market_day(db, day, [200.0], is_synthetic=False)
    _add_indexed_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), margin=0.1)
    _add_hour(db, station, t0, load=10, pv=0, grid_import=10, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["tariff_buy_provenance"] == {"fixed_contract": 0, "indexed_settled": 1, "indexed_synthetic": 0, "unknown": 0}
    assert result["tariff_provenance_summary"] == "measured"


def test_estimated_savings_tariff_provenance_indexed_synthetic_is_not_measured(db):
    """Pretul PZU vine dintr-un fixture SINTETIC (date de test/demo, nu OPCOM
    real) -- trebuie marcat explicit 'estimated', nu prezentat ca masurat."""
    station = _station(db, "provsynthetic")
    day = date(2020, 6, 3)
    t0 = datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
    make_market_day(db, day, [200.0], is_synthetic=True)
    _add_indexed_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), margin=0.1)
    _add_hour(db, station, t0, load=10, pv=0, grid_import=10, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=1))

    assert result["tariff_buy_provenance"] == {"fixed_contract": 0, "indexed_settled": 0, "indexed_synthetic": 1, "unknown": 0}
    assert result["tariff_provenance_summary"] == "estimated"


def test_estimated_savings_hours_export_price_missing_is_reported(db):
    """O ora cu export si tarif necunoscut este exclusa, nu evaluata la zero."""
    station = _station(db, "noexporttariff")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    # Prima ora ramane evaluabila, a doua are export cu pret necunoscut.
    _add_hour(db, station, t0, load=5, pv=5, grid_import=0, grid_export=0)
    _add_hour(db, station, t0 + timedelta(hours=1), load=5, pv=8, grid_import=0, grid_export=3)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=2))

    assert result["available"] is True
    assert result["hours_export_price_missing"] == 1
    assert result["hours_priced"] == 1
    assert result["export_revenue_lei"] == 0.0


def test_estimated_savings_excludes_incomplete_energy_instead_of_zero_filling(db):
    station = _station(db, "incomplete-energy")
    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    _add_tariff_version(db, station, "import", valid_from=t0 - timedelta(days=1), fixed_price="1.0")
    _add_hour(db, station, t0, load=5, pv=2, grid_import=3, grid_export=0)
    _add_hour(db, station, t0 + timedelta(hours=1), load=5, pv=None, grid_import=3, grid_export=0)
    db.commit()

    result = dashboard.get_estimated_savings(db, station, t0, t0 + timedelta(hours=2))

    assert result["available"] is True
    assert result["hours_priced"] == 1
    assert result["hours_with_incomplete_energy_data"] == 1


# --- get_heatmap ---------------------------------------------------------


def test_heatmap_uses_15m_slots_and_returns_average_load_kw(db, monkeypatch):
    station = _station(db, "heatmap", timezone="UTC")
    now = datetime(2026, 9, 17, 12, tzinfo=UTC)
    monkeypatch.setattr(dashboard, "utcnow", lambda: now)

    slot_start = datetime(2026, 9, 15, 10, 15, tzinfo=UTC)
    _add_15m_load(db, station, slot_start, load_kwh=Decimal("0.50"))
    _add_15m_load(db, station, slot_start - timedelta(weeks=1), load_kwh=Decimal("1.00"))
    _add_15m_load(db, station, slot_start + timedelta(minutes=15), load_kwh=Decimal("9.00"), coverage=0.5)
    _add_hour(db, station, slot_start.replace(minute=0), load=99, pv=0, grid_import=99, grid_export=0)
    db.commit()

    result = dashboard.get_heatmap(db, station)

    assert result == [{"weekday": 1, "slot": 41, "avg_load_kw": 3.0}]


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
        source="test", source_version="pv-test-v2", predicted_power_kw=Decimal("2.5"), scenario="expected",
        confidence="low", is_synthetic=True,  # mai recenta, tot inainte de t
    ))
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "pv", t - timedelta(hours=1), t + timedelta(hours=1))

    assert len(out) == 1
    assert abs(out[0]["forecast_kw"] - 2.5) < 0.001
    assert out[0]["is_synthetic"] is True
    assert out[0]["forecast_confidence"] == "low"
    assert out[0]["forecast_source"] == "test"
    assert out[0]["forecast_source_version"] == "pv-test-v2"
    assert out[0]["forecast_issued_at"] == (t - timedelta(hours=1)).isoformat()


def test_forecast_vs_actual_averages_actual_over_forecast_interval(db):
    station = _station(db, "actual-hourly")
    t = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)

    db.add(PvForecast(
        station_id=station.id, issued_at=t - timedelta(hours=1), interval_start=t, interval_end=t + timedelta(hours=1),
        source="test", predicted_power_kw=Decimal("2.0"), scenario="expected",
    ))
    for offset, pv_kwh in enumerate(("1.0000", "0.0000", "0.0000", "0.0000")):
        start = t + timedelta(minutes=15 * offset)
        db.add(
            TelemetryAggregate(
                station_id=station.id,
                period_type="interval_15m",
                period_start=start,
                period_end=start + timedelta(minutes=15),
                pv_energy_kwh=Decimal(pv_kwh),
                coverage={"pv": 1.0},
            )
        )
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "pv", t - timedelta(hours=1), t + timedelta(hours=1))

    assert len(out) == 1
    assert abs(out[0]["actual_kw"] - 1.0) < 0.001


def test_forecast_vs_actual_keeps_actual_null_when_forecast_interval_coverage_is_low(db):
    station = _station(db, "actual-missing")
    t = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)

    db.add(PvForecast(
        station_id=station.id, issued_at=t - timedelta(hours=1), interval_start=t, interval_end=t + timedelta(hours=1),
        source="test", predicted_power_kw=Decimal("2.0"), scenario="expected",
    ))
    db.add(
        TelemetryAggregate(
            station_id=station.id,
            period_type="interval_15m",
            period_start=t,
            period_end=t + timedelta(minutes=15),
            pv_energy_kwh=Decimal("1.0000"),
            coverage={"pv": 1.0},
        )
    )
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "pv", t - timedelta(hours=1), t + timedelta(hours=1))

    assert len(out) == 1
    assert out[0]["actual_kw"] is None


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
    assert out[0]["forecast_confidence"] == "nominal"
    assert out[0]["forecast_source"] == "test"


def test_forecast_vs_actual_pv_exposes_weather_context(db):
    station = _station(db, "pv-weather-context")
    t = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    weather = WeatherForecast(
        station_id=station.id,
        issued_at=t - timedelta(hours=3),
        interval_start=t,
        interval_end=t + timedelta(hours=1),
        source="open-meteo",
        source_version="test-model",
        ghi_w_m2=700.0,
        dni_w_m2=600.0,
        dhi_w_m2=120.0,
        cloud_cover_percent=25.0,
        temperature_c=28.5,
        precipitation_mm=0.2,
        wind_speed_ms=3.4,
    )
    db.add(weather)
    db.flush()
    db.add(PvForecast(
        station_id=station.id, issued_at=t - timedelta(hours=1), interval_start=t, interval_end=t + timedelta(hours=1),
        source="test", predicted_power_kw=Decimal("4.0"), scenario="expected", based_on_weather_forecast_id=weather.id,
    ))
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "pv", t, t + timedelta(hours=1))

    assert out[0]["weather"]["source"] == "open-meteo"
    assert out[0]["weather"]["ghi_w_m2"] == 700.0
    assert out[0]["weather"]["cloud_cover_percent"] == 25.0


def test_forecast_vs_actual_future_load_uses_latest_forecast_batch(db):
    station = _station(db, "load-future")
    t = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    older = t - timedelta(hours=3)
    latest = t - timedelta(minutes=5)

    db.add(ConsumptionForecast(
        station_id=station.id, issued_at=older, interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", base_load_kw=Decimal("0.5"), ev_component_kw=Decimal("0"), flexible_component_kw=Decimal("0"),
        is_cold_start=False,
    ))
    db.add(ConsumptionForecast(
        station_id=station.id, issued_at=latest, interval_start=t, interval_end=t + timedelta(minutes=15),
        source="test", base_load_kw=Decimal("1.4"), ev_component_kw=Decimal("0.2"), flexible_component_kw=Decimal("0.1"),
        is_cold_start=False,
    ))
    db.commit()

    out = dashboard.get_forecast_vs_actual(db, station, "load", t, t + timedelta(minutes=15))

    assert len(out) == 1
    assert abs(out[0]["forecast_kw"] - 1.7) < 0.001
    assert out[0]["forecast_issued_at"] == latest.isoformat()


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


# --- get_energy_period_kpis: comparatie "aceeasi durata scursa" ---------


def test_energy_kpi_today_comparison_uses_elapsed_window_not_full_previous_day(db):
    """Regression (raport client): 'comparatie cu ieri: indisponibila' aparea
    aproape tot timpul zilei, pentru ca pragul de acoperire (90%) se aplica
    fata de coverage-ul agregatului `day`, relativ la ziua INTREAGA (24h),
    nu fata de cat a trecut deja din ea -- la pranz, o zi in curs cu date
    PERFECTE are coverage=0.5, deci nu putea trece niciodata pragul inainte
    de seara. Fix: comparatia foloseste fereastra "aceeasi durata scursa"
    din ziua anterioara (`_elapsed_window_totals`), nu randul `day` complet
    al zilei anterioare."""
    from freezegun import freeze_time

    station = _station(db, "kpi-elapsed")
    tz = ZoneInfo(station.timezone)
    local_noon = datetime(2026, 1, 15, 12, 0, tzinfo=tz)
    now_utc = local_noon.astimezone(UTC)
    today_start = datetime(2026, 1, 15, 0, 0, tzinfo=tz).astimezone(UTC)
    today_end = datetime(2026, 1, 16, 0, 0, tzinfo=tz).astimezone(UTC)
    yesterday_start = datetime(2026, 1, 14, 0, 0, tzinfo=tz).astimezone(UTC)
    yesterday_noon = datetime(2026, 1, 14, 12, 0, tzinfo=tz).astimezone(UTC)

    # "Astazi", exact simptomul raportat: date PERFECTE pentru jumatatea de
    # zi deja scursa, dar coverage=0.5 fata de ziua INTREAGA (24h).
    db.add(TelemetryAggregate(
        station_id=station.id, period_type="day", period_start=today_start, period_end=today_end,
        pv_energy_kwh=Decimal("6.0"), load_energy_kwh=Decimal("4.0"),
        grid_import_energy_kwh=Decimal("1.0"), grid_export_energy_kwh=Decimal("0.5"),
        battery_charge_energy_kwh=Decimal("0.5"), battery_discharge_energy_kwh=Decimal("0.2"),
        sample_count=48, data_quality="measured",
        coverage={"pv": 0.5, "load": 0.5, "grid": 0.5, "battery": 0.5},
    ))
    # Ieri, aceeasi fereastra scursa (00:00-12:00): complet acoperita.
    db.add(TelemetryAggregate(
        station_id=station.id, period_type="interval_15m", period_start=yesterday_start, period_end=yesterday_noon,
        pv_energy_kwh=Decimal("5.0"), load_energy_kwh=Decimal("3.5"),
        grid_import_energy_kwh=Decimal("0.8"), grid_export_energy_kwh=Decimal("0.4"),
        battery_charge_energy_kwh=Decimal("0.3"), battery_discharge_energy_kwh=Decimal("0.1"),
        sample_count=48, data_quality="measured",
        coverage={"pv": 1.0, "load": 1.0, "grid": 1.0, "battery": 1.0},
    ))
    db.commit()

    with freeze_time(now_utc):
        result = dashboard.get_energy_period_kpis(db, station)

    today = result["today"]["metrics"]
    assert today["pv"]["comparison"] is not None
    assert abs(today["pv"]["comparison"]["previous_value"] - 5.0) < 0.001
    assert abs(today["pv"]["comparison"]["delta"] - 1.0) < 0.001
    assert today["grid_import"]["comparison"] is not None
    assert abs(today["grid_import"]["comparison"]["previous_value"] - 0.8) < 0.001


def test_energy_kpi_comparison_stays_unavailable_when_elapsed_window_itself_lacks_data(db):
    """Fix-ul de mai sus schimba BAZA pragului de acoperire, nu il elimina:
    daca portiunea deja scursa din ziua curenta chiar nu are date suficiente,
    comparatia trebuie sa ramana indisponibila, nu sa arate o cifra
    inventata."""
    from freezegun import freeze_time

    station = _station(db, "kpi-elapsed-insufficient")
    tz = ZoneInfo(station.timezone)
    local_noon = datetime(2026, 1, 15, 12, 0, tzinfo=tz)
    now_utc = local_noon.astimezone(UTC)
    today_start = datetime(2026, 1, 15, 0, 0, tzinfo=tz).astimezone(UTC)
    today_end = datetime(2026, 1, 16, 0, 0, tzinfo=tz).astimezone(UTC)

    # coverage=0.1 fata de ziua intreaga -> rescalat la portiunea scursa
    # (12h din 24h), tot sub pragul de 90%.
    db.add(TelemetryAggregate(
        station_id=station.id, period_type="day", period_start=today_start, period_end=today_end,
        pv_energy_kwh=Decimal("1.0"), load_energy_kwh=Decimal("1.0"),
        grid_import_energy_kwh=Decimal("0.1"), grid_export_energy_kwh=Decimal("0.1"),
        battery_charge_energy_kwh=Decimal("0.1"), battery_discharge_energy_kwh=Decimal("0.1"),
        sample_count=5, data_quality="estimated",
        coverage={"pv": 0.1, "load": 0.1, "grid": 0.1, "battery": 0.1},
    ))
    db.commit()

    with freeze_time(now_utc):
        result = dashboard.get_energy_period_kpis(db, station)

    today = result["today"]["metrics"]
    assert today["pv"]["comparison"] is None
    assert today["pv"]["value"] == 1.0  # valoarea ramane afisata, doar comparatia lipseste


def test_energy_kpi_month_comparison_uses_elapsed_window_not_full_previous_month(db):
    """Acelasi fix, la granularitatea lunii: comparatia cu luna anterioara
    foloseste zilele agregate ale lunii trecute corespunzatoare portiunii
    deja scurse din luna curenta, nu randul `month` complet, incheiat."""
    from freezegun import freeze_time

    station = _station(db, "kpi-elapsed-month")
    tz = ZoneInfo(station.timezone)
    # 10 zile scurse din luna curenta (ianuarie).
    now_local = datetime(2026, 1, 11, 0, 0, tzinfo=tz)
    now_utc = now_local.astimezone(UTC)
    month_start = datetime(2026, 1, 1, tzinfo=tz).astimezone(UTC)
    month_end = datetime(2026, 2, 1, tzinfo=tz).astimezone(UTC)
    prev_month_start = datetime(2025, 12, 1, tzinfo=tz).astimezone(UTC)
    prev_month_elapsed_end = datetime(2025, 12, 11, 0, 0, tzinfo=tz).astimezone(UTC)

    # Coverage=1/3 fata de luna intreaga (31 zile), desi cele 10 zile scurse
    # au date complete -- exact acelasi simptom structural ca la "azi".
    db.add(TelemetryAggregate(
        station_id=station.id, period_type="month", period_start=month_start, period_end=month_end,
        pv_energy_kwh=Decimal("50.0"), load_energy_kwh=Decimal("40.0"),
        grid_import_energy_kwh=None, grid_export_energy_kwh=None,
        battery_charge_energy_kwh=None, battery_discharge_energy_kwh=None,
        sample_count=10, data_quality="measured",
        coverage={"pv": 10 / 31, "load": 10 / 31, "grid": 0.0, "battery": 0.0},
    ))
    # Luna trecuta (decembrie), primele 10 zile: un singur rand `day`
    # agregat pentru simplitate, insumand cele 10 zile.
    db.add(TelemetryAggregate(
        station_id=station.id, period_type="day", period_start=prev_month_start, period_end=prev_month_elapsed_end,
        pv_energy_kwh=Decimal("45.0"), load_energy_kwh=Decimal("38.0"),
        grid_import_energy_kwh=None, grid_export_energy_kwh=None,
        battery_charge_energy_kwh=None, battery_discharge_energy_kwh=None,
        sample_count=10, data_quality="measured",
        coverage={"pv": 1.0, "load": 1.0, "grid": 0.0, "battery": 0.0},
    ))
    db.commit()

    with freeze_time(now_utc):
        result = dashboard.get_energy_period_kpis(db, station)

    month = result["month"]["metrics"]
    assert month["pv"]["comparison"] is not None
    assert abs(month["pv"]["comparison"]["previous_value"] - 45.0) < 0.001
    assert abs(month["pv"]["comparison"]["delta"] - 5.0) < 0.001
