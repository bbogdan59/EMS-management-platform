from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.security import utcnow
from app.models.enums import ExecutionMode, OptimizationRunStatus, PlanStatus
from app.models.forecast import ConsumptionForecast, PvForecast
from app.models.optimization import Plan, PlanInterval
from app.models.preference import PreferenceVersion
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services.optimization_service import OptimizationLockedError, run_optimization_for_station
from tests.factories import make_device, make_market_day, make_org, make_station, make_user


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


def _add_tariffs_with_terminal_drop(db, station, *, high_price, low_price, cutover):
    """Ca `_add_tariffs`, dar cu pretul de import scazand la `low_price` incepand
    de la `cutover`. Termenul de "valoare terminala" din obiectiv e calibrat
    dupa pretul ULTIMULUI interval din orizont -- cu un pret CONSTANT pe tot
    orizontul, pastrarea SOC pana la final valoreaza aproape la fel de mult cat
    descarcarea imediata, anuland aproape complet beneficiul economic testat
    aici. Scaderea pretului spre finalul orizontului elimina acest artefact
    fara sa afecteze stimulentul de descarcare in perioada relevanta testului."""
    imp = Tariff(station_id=station.id, direction="import", kind="fixed", name="t-import")
    db.add(imp)
    db.flush()
    db.add(
        TariffVersion(
            tariff_id=imp.id, valid_from=utcnow() - timedelta(days=1),
            fixed_price_lei_per_kwh=Decimal(high_price), fixed_monthly_fee_lei=Decimal("0"),
            variable_component_lei_per_kwh=Decimal("0"),
        )
    )
    db.add(
        TariffVersion(
            tariff_id=imp.id, valid_from=cutover,
            fixed_price_lei_per_kwh=Decimal(low_price), fixed_monthly_fee_lei=Decimal("0"),
            variable_component_lei_per_kwh=Decimal("0"),
        )
    )
    exp = Tariff(station_id=station.id, direction="export", kind="fixed", name="t-export")
    db.add(exp)
    db.flush()
    db.add(
        TariffVersion(
            tariff_id=exp.id, valid_from=utcnow() - timedelta(days=1),
            fixed_price_lei_per_kwh=Decimal("0.1"), fixed_monthly_fee_lei=Decimal("0"),
            variable_component_lei_per_kwh=Decimal("0"),
        )
    )
    db.flush()


def _add_forecasts(db, station, hours=40):
    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(hours):
        t = start + timedelta(hours=h)
        hour = t.astimezone(UTC).hour
        pv_kw = max(0.0, 4.0 * (1 - abs(hour - 13) / 7)) if 6 <= hour <= 20 else 0.0
        db.add(PvForecast(station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1), source="test", predicted_power_kw=Decimal(str(round(pv_kw, 3))), scenario="expected"))
    for q in range(hours * 4):
        t = start + timedelta(minutes=15 * q)
        load = 0.5 if t.astimezone(UTC).hour < 6 else 1.2
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


def test_optimization_fallback_when_no_forecasts_available(db, monkeypatch):
    from app.services import weather_service
    from app.services.weather_service import WeatherUnavailableError

    # Fortam explicit absenta ambelor prognoze (PV si consum), in loc sa ne
    # bazam pe indisponibilitatea retelei din mediul de test: PV forecast
    # depinde la randul lui de existenta unor randuri WeatherForecast, deci
    # daca refresh-ul meteo ar reusi (ex. intr-un mediu CI cu acces real la
    # retea), testul ar deveni nedeterminist -- a picat exact asa in CI.
    def _always_unavailable(*args, **kwargs):
        raise WeatherUnavailableError("simulat indisponibil pentru test")

    monkeypatch.setattr(weather_service, "refresh_weather_for_station", _always_unavailable)

    user = make_user(db, email="opt4@test.local")
    org = make_org(db, "Opt Org 4")
    station = make_station(db, org, user, name="Opt Station 4")
    _add_tariffs(db, station)
    db.commit()  # fara prognoze PV/consum

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.is_fallback is True
    assert "Prognoze" in run.fallback_reason or "prognoz" in run.fallback_reason.lower()


# --- Regresii pentru issue #9: prospetime SOC, proveniența pretului, ---
# --- aliniere prognoza consum, snapshot reproductibil, versionare si ---
# --- concurenta pana la commit. ------------------------------------------


def _add_soc(db, station, *, age_minutes: float, percent: str = "60") -> TelemetryRaw:
    device = make_device(db, station)
    now = utcnow()
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="boot-1", sequence=1,
            measured_at=now - timedelta(minutes=age_minutes), received_at=now,
            battery_soc_percent=Decimal(percent),
        )
    )
    db.flush()
    return device


def _round_to_interval_for_test(dt):
    discard = timedelta(minutes=dt.minute % 15, seconds=dt.second, microseconds=dt.microsecond)
    return (dt - discard) + timedelta(minutes=15)


def test_stale_soc_blocks_live_plan_but_not_shadow(db):
    user = make_user(db, email="opt-soc@test.local")
    org = make_org(db, "Opt SOC Org")
    station = make_station(db, org, user, name="Opt SOC Station")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    _add_soc(db, station, age_minutes=45)  # depaseste optimization_soc_max_age_minutes (10 implicit)
    db.commit()

    # Statia ramane in modul implicit "shadow": SOC vechi nu blocheaza planul,
    # dar informatia trebuie sa fie vizibila in snapshot drept "stale", nu ascunsa.
    run_shadow = run_optimization_for_station(db, station.id, triggered_by="user")
    assert run_shadow.is_fallback is False
    assert run_shadow.input_snapshot["soc"]["quality"] == "stale"

    station.execution_mode = ExecutionMode.live.value
    db.add(station)
    db.commit()

    run_live = run_optimization_for_station(db, station.id, triggered_by="user")
    assert run_live.is_fallback is True
    assert "soc" in run_live.fallback_reason.lower() or "SOC" in run_live.fallback_reason


def test_missing_soc_blocks_live_plan(db):
    user = make_user(db, email="opt-soc-missing@test.local")
    org = make_org(db, "Opt SOC Missing Org")
    station = make_station(db, org, user, name="Opt SOC Missing Station")
    station.execution_mode = ExecutionMode.live.value
    db.add(station)
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    db.commit()  # nicio telemetrie SOC inregistrata vreodata

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    assert run.is_fallback is True
    assert run.input_snapshot == {}  # fallback nu publica un snapshot de succes
    assert "SOC" in run.fallback_reason or "soc" in run.fallback_reason.lower()


def test_price_gaps_are_time_local_filled_and_marked_estimated(db):
    """Reproduce exact problema semnalata: un pret disponibil undeva in orizont
    nu trebuie folosit orb pentru toate golurile -- completarea trebuie sa fie
    din cel mai apropiat interval cunoscut in timp, iar fiecare interval
    completat trebuie marcat explicit "estimated" in snapshot. Ceas fix (ca la
    testele de piata din test_market_analytics.py): orizontul de 36h trebuie sa
    acopere exact doua zile calendaristice UTC cunoscute, nu un numar
    nedeterminist in functie de cand ruleaza testul cu adevarat."""
    from freezegun import freeze_time

    with freeze_time("2026-03-10 08:00:00"):
        user = make_user(db, email="opt-pricegap@test.local")
        org = make_org(db, "Opt Pricegap Org")
        station = make_station(db, org, user, name="Opt Pricegap Station")
        station.execution_mode = ExecutionMode.live.value
        db.add(station)
        _add_soc(db, station, age_minutes=1)

        tariff = Tariff(station_id=station.id, direction="import", kind="indexed_opcom", name="import-opcom")
        db.add(tariff)
        db.flush()
        db.add(
            TariffVersion(
                tariff_id=tariff.id, valid_from=utcnow() - timedelta(days=1),
                opcom_margin_lei_per_kwh=Decimal("0.1"), fixed_monthly_fee_lei=Decimal("0"),
                variable_component_lei_per_kwh=Decimal("0"),
            )
        )
        export_tariff = Tariff(station_id=station.id, direction="export", kind="fixed", name="export-fixed")
        db.add(export_tariff)
        db.flush()
        db.add(
            TariffVersion(
                tariff_id=export_tariff.id, valid_from=utcnow() - timedelta(days=1),
                fixed_price_lei_per_kwh=Decimal("0.35"), fixed_monthly_fee_lei=Decimal("0"),
                variable_component_lei_per_kwh=Decimal("0"),
            )
        )
        _add_forecasts(db, station)

        # Piata reala acopera doar prima zi a orizontului (96 intervale de 15 min la 500 lei/MWh);
        # a doua zi ramane fara nicio publicare reala -- exact scenariul "pret maine nepublicat".
        make_market_day(db, utcnow().date(), [500.0] * 96, source="opcom_pzu_gap_test")
        db.commit()

        run = run_optimization_for_station(db, station.id, triggered_by="user")

        assert run.is_fallback is False
        quality = run.input_snapshot["price_buy_quality"]
        assert "real" in quality.values()
        assert "estimated" in quality.values()
        assert run.input_snapshot["price_estimated_count"] > 0

        # Planul nu poate ramane live cand contine preturi estimate: retrogradat la shadow,
        # cu motivul documentat, in loc sa fie tratat tacit ca live.
        plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
        assert plan.execution_mode == ExecutionMode.shadow.value
        assert "estimat" in run.explanation_summary.lower()


def test_synthetic_market_price_is_never_treated_as_real(db):
    """O fixtura sintetica (demo/diagnostic) nu trebuie sa alimenteze niciodata
    pretul folosit intr-un plan -- trebuie tratata identic cu absenta datelor,
    nu ca sursa reala, indiferent daca e singura valoare "disponibila"."""
    from freezegun import freeze_time

    with freeze_time("2026-03-10 08:00:00"):
        user = make_user(db, email="opt-synthetic@test.local")
        org = make_org(db, "Opt Synthetic Org")
        station = make_station(db, org, user, name="Opt Synthetic Station")
        station.execution_mode = ExecutionMode.live.value
        db.add(station)
        _add_soc(db, station, age_minutes=1)

        tariff = Tariff(station_id=station.id, direction="import", kind="indexed_opcom", name="import-opcom")
        db.add(tariff)
        db.flush()
        db.add(
            TariffVersion(
                tariff_id=tariff.id, valid_from=utcnow() - timedelta(days=1),
                opcom_margin_lei_per_kwh=Decimal("0.1"), fixed_monthly_fee_lei=Decimal("0"),
                variable_component_lei_per_kwh=Decimal("0"),
            )
        )
        _add_forecasts(db, station)

        today_utc = utcnow().date()
        tomorrow_utc = today_utc + timedelta(days=1)
        # Real doar pentru prima zi; a doua zi are DOAR o fixtura sintetica -- trebuie
        # tratata ca lipsa, deci completata prin extrapolare din ziua reala si marcata "estimated".
        make_market_day(db, today_utc, [500.0] * 96, source="opcom_pzu_real_test")
        make_market_day(db, tomorrow_utc, [50.0] * 96, is_synthetic=True, source="opcom_pzu_synthetic_test")
        db.commit()

        run = run_optimization_for_station(db, station.id, triggered_by="user")

        assert run.is_fallback is False
        # Daca fixtura sintetica (50 lei/MWh = 0.05+0.1 = 0.15 lei/kWh) ar fi fost folosita ca reala,
        # ea ar fi aparut ca "real" in a doua zi in loc de "estimated" propagat din prima zi.
        quality = run.input_snapshot["price_buy_quality"]
        prices = run.input_snapshot["price_buy_lei_kwh"]
        synthetic_day_keys = [k for k in quality if datetime.fromisoformat(k).astimezone(UTC).date() == tomorrow_utc]
        assert synthetic_day_keys
        assert all(quality[k] == "estimated" for k in synthetic_day_keys)
        assert all(abs(prices[k] - 0.15) > 0.01 for k in synthetic_day_keys)


def test_input_snapshot_is_sufficient_for_replay(db):
    user = make_user(db, email="opt-snapshot@test.local")
    org = make_org(db, "Opt Snapshot Org")
    station = make_station(db, org, user, name="Opt Snapshot Station")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    _add_soc(db, station, age_minutes=1)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")

    snap = run.input_snapshot
    assert snap["schema_version"] == 1
    assert snap["horizon"]["n_intervals"] == len(snap["pv_forecast_kw"]) == len(snap["load_forecast_kw"])
    assert snap["horizon"]["interval_minutes"] == 15
    assert snap["soc"]["quality"] == "measured"
    assert snap["soc"]["measured_at"] is not None
    assert snap["config"]["id"] == str(run.station_config_version_id)
    assert snap["preference"]["id"] == str(run.preference_version_id)
    assert set(snap["pv_forecast_raw_kw"]) == set(snap["pv_forecast_kw"])
    assert set(snap["load_forecast_raw_kw"]) == set(snap["load_forecast_kw"])
    assert set(snap["price_buy_lei_kwh"].keys()) == set(snap["price_buy_quality"].keys())
    # Fiecare cheie de orizont e reproductibila ca timestamp UTC explicit.
    for key in list(snap["pv_forecast_kw"])[:3]:
        datetime.fromisoformat(key)


def test_optimization_service_does_not_commit_callers_transaction(monkeypatch):
    from unittest.mock import MagicMock

    from app.services import optimization_service

    db = MagicMock()
    expected = object()
    lock = MagicMock()
    monkeypatch.setattr(optimization_service, "_acquire_lock", lambda _station_id: lock)
    monkeypatch.setattr(optimization_service, "_run_locked", lambda *_args: expected)

    result = run_optimization_for_station(db, uuid.uuid4(), triggered_by="user")

    assert result is expected
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    lock.release.assert_called_once()


def test_plan_version_does_not_restart_after_previous_plan_completed(db):
    user = make_user(db, email="opt-version@test.local")
    org = make_org(db, "Opt Version Org")
    station = make_station(db, org, user, name="Opt Version Station")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    db.commit()

    first_run = run_optimization_for_station(db, station.id, triggered_by="user")
    first_plan = db.scalar(select(Plan).where(Plan.optimization_run_id == first_run.id))
    assert first_plan.version == 1

    # Simuleaza inchiderea completa a ciclului de viata al planului (executat integral).
    first_plan.status = PlanStatus.completed.value
    db.add(first_plan)
    db.commit()

    second_run = run_optimization_for_station(db, station.id, triggered_by="user")
    second_plan = db.scalar(select(Plan).where(Plan.optimization_run_id == second_run.id))

    assert second_plan.version == 2, "versiunea nu trebuie sa reinceapa de la 1 dupa un plan inchis (completed)"


def test_consumption_forecast_alignment_matches_optimization_grid(db):
    """Reproduce bug-ul: daca `_ensure_forecasts` genereaza prognoza de consum
    pornind de la un moment nealiniat la grila de 15 minute a orizontului,
    `_build_load_series` nu gaseste nicio potrivire exacta si intregul consum
    cade pe valoarea implicita de fallback. Fara nicio prognoza de consum
    pre-populata manual: se bazeaza exclusiv pe generarea reala din istoric."""
    user = make_user(db, email="opt-align@test.local")
    org = make_org(db, "Opt Align Org")
    station = make_station(db, org, user, name="Opt Align Station")
    _add_tariffs(db, station)

    # Istoric de telemetrie agregata (cold-start e suficient -- doar aliniere se testeaza).
    start = utcnow() - timedelta(days=2)
    for q in range(4 * 24 * 2):
        t = start + timedelta(minutes=15 * q)
        db.add(
            TelemetryAggregate(
                station_id=station.id, period_type="interval_15m", period_start=t, period_end=t + timedelta(minutes=15),
                load_energy_kwh=Decimal("0.3"), coverage={"load": 1.0},
            )
        )
    db.flush()

    # PV: fara istoric real, dar generarea PV e best-effort si tolerata la esec (vezi _ensure_forecasts);
    # ce conteaza aici e coverage-ul de consum, generat exclusiv din TelemetryAggregate de mai sus.
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")

    assert run.input_snapshot["load_coverage"] == run.input_snapshot["horizon"]["n_intervals"], (
        "toate intervalele orizontului trebuie sa gaseasca o prognoza de consum aliniata exact"
    )


def test_forecast_refresh_failure_does_not_poison_session(db, monkeypatch):
    """Un esec in timpul unui refresh best-effort (meteo/PV/consum) nu trebuie
    sa lase sesiunea SQLAlchemy intr-o stare inutilizabila pentru restul
    calculului -- vezi SAVEPOINT-urile dedicate din `_ensure_forecasts`."""
    from sqlalchemy import text

    from app.services import weather_service

    def _break_transaction(*args, **kwargs):
        db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(weather_service, "refresh_weather_for_station", _break_transaction)

    user = make_user(db, email="opt-session@test.local")
    org = make_org(db, "Opt Session Org")
    station = make_station(db, org, user, name="Opt Session Station")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")

    assert run.status == OptimizationRunStatus.succeeded.value
    assert run.is_fallback is False
    # Sesiunea ramane utilizabila dupa esec: aceasta interogare ar ridica
    # InvalidRequestError daca tranzactia ar fi ramas invalidata.
    assert db.scalar(select(Plan).where(Plan.optimization_run_id == run.id)) is not None


def test_concurrent_optimization_runs_serialize_until_commit(engine):
    """Lock-ul Redis trebuie tinut pe toata durata calculului SI a commit-ului.
    Doua apeluri concurente reale (sesiuni/conexiuni separate, engine-ul de test
    real, nu fixtura `db` cu SAVEPOINT care nu poate exercita commit-uri
    concurente reale) trebuie sa produca exact un plan reusit plus fie un al
    doilea plan cu versiune secventiala corecta (daca a doua incercare a asteptat
    si a rulat dupa commit-ul primei), fie un `OptimizationLockedError` curat --
    niciodata o eroare bruta de constrangere unica la commit."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.models.organization import Organization
    from app.models.user import User

    suffix = uuid.uuid4().hex
    with Session(engine) as setup:
        user = make_user(setup, email=f"{suffix}@concurrency.test")
        org = make_org(setup, f"Concurrency Opt {suffix}")
        station = make_station(setup, org, user, name=f"Concurrency Opt Station {suffix}")
        _add_tariffs(setup, station)
        _add_forecasts(setup, station)
        setup.commit()
        station_id, user_id, org_id = station.id, user.id, org.id

    barrier = Barrier(2)

    def attempt():
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                run = run_optimization_for_station(session, station_id, triggered_by="user")
                session.commit()
                return ("ok", run.id)
            except OptimizationLockedError:
                return ("locked", None)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            outcomes = [f.result(timeout=30) for f in futures]

        oks = [o for o in outcomes if o[0] == "ok"]
        assert len(oks) >= 1, "cel putin o incercare trebuie sa reuseasca"
        assert all(o[0] in ("ok", "locked") for o in outcomes), "nicio eroare bruta de constrangere unica"

        with Session(engine) as verify:
            plans = verify.scalars(
                select(Plan).where(Plan.station_id == station_id).order_by(Plan.version)
            ).all()
            versions = [p.version for p in plans]
            assert versions == list(range(1, len(versions) + 1)), "versiunile trebuie sa fie secventiale, fara coliziuni"
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


# --- Regresii pentru issue #12: buget baterie/EFC, limite fizice, tinte SOC, EV. ---


def test_soc_recovers_from_out_of_band_without_infeasibility_or_energy_fabrication(db):
    """SOC masurat sub banda minima de rezerva (15% implicit) nu trebuie sa faca
    planul infezabil si nu trebuie "teleportat" artificial in banda -- dinamica
    SOC raportata trebuie sa respecte exact puterea de incarcare/descarcare
    aleasa de solver (nicio energie inventata/disparuta)."""
    user = make_user(db, email="opt-socband@test.local")
    org = make_org(db, "Opt SOC Band Org")
    station = make_station(db, org, user, name="Opt SOC Band Station")
    _add_tariffs(db, station)
    _add_forecasts(db, station)
    _add_soc(db, station, age_minutes=1, percent="5")  # sub pragul minim de rezerva (15%)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.status == OptimizationRunStatus.succeeded.value
    assert run.is_fallback is False

    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(
        select(PlanInterval).where(PlanInterval.plan_id == plan.id).order_by(PlanInterval.interval_start)
    ).all()
    first = intervals[0]

    avail_capacity_kwh = 10.0  # battery_available_capacity_kwh implicit din factory
    starting_soc_kwh = 0.05 * avail_capacity_kwh
    eff, dt_h = 0.95, 0.25
    batt = float(first.battery_power_target_kw)
    expected_delta_kwh = (batt * eff if batt >= 0 else batt / eff) * dt_h
    expected_soc_kwh = starting_soc_kwh + expected_delta_kwh
    actual_soc_kwh = float(first.battery_soc_target_percent) / 100.0 * avail_capacity_kwh
    assert abs(actual_soc_kwh - expected_soc_kwh) < 0.01, (
        "SOC-ul rezultat trebuie sa respecte exact dinamica fizica declarata de puterea bateriei"
    )

    # Nicio "teleportare" instantanee in banda: cresterea intr-un singur interval e
    # limitata fizic de puterea maxima de incarcare (3 kW implicit din factory).
    max_possible_increase = 3.0 * eff * dt_h
    assert actual_soc_kwh <= starting_soc_kwh + max_possible_increase + 0.01


def test_ev_forecast_component_not_double_counted_in_optimizer_load(db):
    """`ev_component_kw` din prognoza de consum (medie istorica pasiva) nu
    trebuie insumat in consumul folosit de optimizator -- ar insemna dubla
    numarare fata de propria variabila de decizie a optimizatorului pentru
    incarcarea EV."""
    user = make_user(db, email="opt-evdc@test.local")
    org = make_org(db, "Opt EV DC Org")
    station = make_station(db, org, user, name="Opt EV DC Station")
    _add_tariffs(db, station)

    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(40):
        t = start + timedelta(hours=h)
        db.add(
            PvForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                source="test", predicted_power_kw=Decimal("0"), scenario="expected",
            )
        )
    for q in range(40 * 4):
        t = start + timedelta(minutes=15 * q)
        db.add(
            ConsumptionForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                source="test", base_load_kw=Decimal("0.5"), ev_component_kw=Decimal("1.5"),
                flexible_component_kw=Decimal("0"), is_cold_start=True,
            )
        )
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.status == OptimizationRunStatus.succeeded.value
    load_raw = run.input_snapshot["load_forecast_raw_kw"]
    assert all(abs(v - 0.5) < 1e-6 for v in load_raw.values() if v is not None), (
        "componenta EV pasiva din prognoza nu trebuie insumata in consumul folosit de optimizator"
    )


def test_efc_daily_budget_deducts_already_realized_usage(db):
    """Bugetul EFC ramas pentru ZIUA CALENDARISTICA LOCALA curenta trebuie sa
    scada utilizarea deja realizata (din TelemetryAggregate orar), nu doar
    bugetul nominal complet."""
    from zoneinfo import ZoneInfo

    from freezegun import freeze_time

    tz = ZoneInfo("Europe/Bucharest")
    frozen_at = datetime(2026, 3, 10, 6, 0, tzinfo=UTC)
    with freeze_time(frozen_at):
        user = make_user(db, email="opt-efcday@test.local")
        org = make_org(db, "Opt EFC Day Org")
        station = make_station(db, org, user, name="Opt EFC Day Station")
        # Pret ridicat azi, scazut spre finalul orizontului -- vezi docstring-ul
        # `_add_tariffs_with_terminal_drop` (neutralizeaza "valoarea terminala").
        _add_tariffs_with_terminal_drop(db, station, high_price="2.0", low_price="0.05", cutover=utcnow() + timedelta(hours=20))

        start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        issued = utcnow()
        for h in range(40):
            t = start + timedelta(hours=h)
            db.add(
                PvForecast(
                    station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                    source="test", predicted_power_kw=Decimal("0"), scenario="expected",
                )
            )
        for q in range(40 * 4):
            t = start + timedelta(minutes=15 * q)
            db.add(
                ConsumptionForecast(
                    station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                    source="test", base_load_kw=Decimal("2.0"), ev_component_kw=Decimal("0"),
                    flexible_component_kw=Decimal("0"), is_cold_start=True,
                )
            )

        _add_soc(db, station, age_minutes=1, percent="90")

        pref = db.scalar(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id))
        pref.max_efc_per_day = Decimal("0.3")  # buget nominal 3 kWh (0.3 * 10 kWh referinta)
        db.add(pref)

        # EFC deja consumat AZI (2.5 kWh), inainte de inceputul orizontului -- ramane
        # doar 0.5 kWh buget pentru restul zilei calendaristice locale.
        for hour in (2, 3, 4, 5):
            t = datetime(2026, 3, 10, hour, tzinfo=UTC)
            db.add(
                TelemetryAggregate(
                    station_id=station.id, period_type="hour", period_start=t, period_end=t + timedelta(hours=1),
                    battery_discharge_energy_kwh=Decimal("0.625"), coverage={"battery": 1.0},
                )
            )
        db.commit()

        run = run_optimization_for_station(db, station.id, triggered_by="user")
        db.commit()

        assert run.status == OptimizationRunStatus.succeeded.value

        plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
        intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()

        today_local = frozen_at.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        local_midnight_next_utc = (today_local + timedelta(days=1)).astimezone(UTC)
        today_discharge_kwh = sum(
            max(-float(pi.battery_power_target_kw), 0) * 0.25
            for pi in intervals if pi.interval_start < local_midnight_next_utc
        )

        assert today_discharge_kwh <= 0.5 + 0.05, (
            f"bugetul EFC zilnic ramas (0.5 kWh dupa scaderea utilizarii deja realizate) "
            f"a fost depasit: {today_discharge_kwh:.3f} kWh descarcati azi"
        )
        assert today_discharge_kwh > 0.2, "testul trebuie sa exercite efectiv constrangerea EFC, nu doar sa treaca trivial"


def test_efc_monthly_budget_uses_local_calendar_month_boundary(db):
    """Bugetul EFC lunar trebuie calculat pe granita LUNII CALENDARISTICE LOCALE,
    nu pe `horizon[0].replace(day=1)` in UTC -- utilizarea din decembrie nu
    trebuie sa reduca bugetul lunii ianuarie doar pentru ca orizontul incepe
    langa miezul noptii, unde UTC si ora locala cad in luni diferite."""
    from freezegun import freeze_time

    frozen_at = datetime(2025, 12, 31, 22, 0, tzinfo=UTC)  # local Bucuresti: 1 ianuarie, 00:00
    with freeze_time(frozen_at):
        user = make_user(db, email="opt-efcmonth@test.local")
        org = make_org(db, "Opt EFC Month Org")
        station = make_station(db, org, user, name="Opt EFC Month Station")
        # Pret ridicat pe aproape tot orizontul, scazut doar spre chiar finalul lui --
        # vezi docstring-ul `_add_tariffs_with_terminal_drop` (neutralizeaza "valoarea terminala").
        _add_tariffs_with_terminal_drop(db, station, high_price="2.0", low_price="0.05", cutover=utcnow() + timedelta(hours=34))

        start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        issued = utcnow()
        for h in range(40):
            t = start + timedelta(hours=h)
            db.add(
                PvForecast(
                    station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                    source="test", predicted_power_kw=Decimal("0"), scenario="expected",
                )
            )
        for q in range(40 * 4):
            t = start + timedelta(minutes=15 * q)
            db.add(
                ConsumptionForecast(
                    station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                    source="test", base_load_kw=Decimal("2.0"), ev_component_kw=Decimal("0"),
                    flexible_component_kw=Decimal("0"), is_cold_start=True,
                )
            )

        _add_soc(db, station, age_minutes=1, percent="90")

        pref = db.scalar(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id))
        pref.max_efc_per_month = Decimal("1.0")  # buget nominal 10 kWh (1.0 * 10 kWh referinta)
        db.add(pref)

        # Descarcare deja realizata in DECEMBRIE (8 kWh) -- nu trebuie scazuta din bugetul
        # lunii calendaristice LOCALE ianuarie, desi orizontul incepe la 22:00 UTC 31 dec.
        for t, amount in [
            (datetime(2025, 12, 30, 10, tzinfo=UTC), "4.0"),
            (datetime(2025, 12, 31, 10, tzinfo=UTC), "4.0"),
        ]:
            db.add(
                TelemetryAggregate(
                    station_id=station.id, period_type="hour", period_start=t, period_end=t + timedelta(hours=1),
                    battery_discharge_energy_kwh=Decimal(amount), coverage={"battery": 1.0},
                )
            )
        db.commit()

        run = run_optimization_for_station(db, station.id, triggered_by="user")
        db.commit()

        assert run.status == OptimizationRunStatus.succeeded.value

        plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
        intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()
        total_discharge_kwh = sum(max(-float(pi.battery_power_target_kw), 0) * 0.25 for pi in intervals)

        # Cu bug-ul vechi (`horizon[0].replace(day=1)` in UTC), cele 8 kWh din decembrie ar fi
        # fost scazute gresit din bugetul lunii ianuarie, limitand descarcarea la 2 kWh pe tot
        # orizontul. Fix-ul foloseste granita LOCALA a lunii -- decembrie nu afecteaza ianuarie.
        assert total_discharge_kwh > 2.5, (
            f"utilizarea EFC din decembrie nu trebuie sa reduca bugetul lunii calendaristice locale "
            f"ianuarie, dar planul a descarcat doar {total_discharge_kwh:.3f} kWh"
        )


def test_soc_target_applies_at_target_moment_not_one_interval_late(db):
    """O tinta SOC la ora X trebuie sa constranga starea EXISTENTA la ora X
    (sfarsitul intervalului anterior), nu sfarsitul intervalului care incepe
    la ora X (asta ar aplica tinta cu un interval intreg mai tarziu)."""
    user = make_user(db, email="opt-soctarget@test.local")
    org = make_org(db, "Opt SOC Target Org")
    station = make_station(db, org, user, name="Opt SOC Target Station")
    _add_tariffs(db, station)

    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(40):
        t = start + timedelta(hours=h)
        db.add(
            PvForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                source="test", predicted_power_kw=Decimal("0"), scenario="expected",
            )
        )
    for q in range(40 * 4):
        t = start + timedelta(minutes=15 * q)
        db.add(
            ConsumptionForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                source="test", base_load_kw=Decimal("0.3"), ev_component_kw=Decimal("0"),
                flexible_component_kw=Decimal("0"), is_cold_start=True,
            )
        )
    _add_soc(db, station, age_minutes=1, percent="20")

    pref = db.scalar(select(PreferenceVersion).where(PreferenceVersion.station_id == station.id))
    pref.allow_grid_charge = True  # elimina orice ambiguitate legata de disponibilitatea PV (0 aici)

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(station.timezone)
    horizon_start = _round_to_interval_for_test(utcnow())
    # `soc_targets[].time` e o ora LOCALA statiei (vezi `_resolve_soc_targets`), nu UTC.
    target_a_time = (horizon_start + timedelta(minutes=15 * 4)).astimezone(tz).strftime("%H:%M")
    target_b_time = (horizon_start + timedelta(minutes=15 * 8)).astimezone(tz).strftime("%H:%M")
    pref.soc_targets = [
        {"time": target_a_time, "days_of_week": None, "target_soc_percent": 40},
        {"time": target_b_time, "days_of_week": None, "target_soc_percent": 60},
    ]
    db.add(pref)
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.status == OptimizationRunStatus.succeeded.value

    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(
        select(PlanInterval).where(PlanInterval.plan_id == plan.id).order_by(PlanInterval.interval_start)
    ).all()

    # Tinta la orizont[4] se aplica lui soc[3] (indexul 3 din plan) -- adica STARII de
    # la orizont[4], nu sfarsitului intervalului 4. Similar pentru orizont[8] -> soc[7].
    assert abs(float(intervals[3].battery_soc_target_percent) - 40.0) < 1.5, (
        "tinta de 40% trebuie atinsa la momentul cerut (sfarsitul intervalului 3), nu cu un interval mai tarziu"
    )
    assert abs(float(intervals[7].battery_soc_target_percent) - 60.0) < 1.5, (
        "tinta de 60% trebuie atinsa la momentul cerut (sfarsitul intervalului 7), nu cu un interval mai tarziu"
    )


def test_pv_curtailment_respects_shared_inverter_limit(db):
    """PV + descarcare baterie, simultan pe partea AC, nu poate depasi puterea
    invertorului -- surplusul de PV peste aceasta limita trebuie curtailat
    (nu doar ignorat), nicaieri in bilantul energetic raportat."""
    user = make_user(db, email="opt-curtail@test.local")
    org = make_org(db, "Opt Curtail Org")
    station = make_station(db, org, user, name="Opt Curtail Station", inverter_power_kw=Decimal("3"))
    _add_tariffs(db, station)

    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(40):
        t = start + timedelta(hours=h)
        db.add(
            PvForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                source="test", predicted_power_kw=Decimal("5"), scenario="expected",  # peste limita invertorului (3 kW)
            )
        )
    for q in range(40 * 4):
        t = start + timedelta(minutes=15 * q)
        db.add(
            ConsumptionForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                source="test", base_load_kw=Decimal("0.5"), ev_component_kw=Decimal("0"),
                flexible_component_kw=Decimal("0"), is_cold_start=True,
            )
        )
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.status == OptimizationRunStatus.succeeded.value

    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()

    inverter_kw = 3.0
    curtailed_somewhere = False
    for pi in intervals:
        pv_raw = float(pi.pv_forecast_kw)
        load = float(pi.load_forecast_kw)
        batt = float(pi.battery_power_target_kw)
        grid = float(pi.grid_power_target_kw)
        ev = float(pi.ev_charge_power_kw)
        discharge = max(-batt, 0)
        # Din bilantul energetic: pv_raw - curtailed + discharge + import == load + charge + export + ev
        # => pv_efectiv (dupa curtailment) = load + batt - grid + ev
        effective_pv = load + batt - grid + ev
        assert effective_pv <= inverter_kw + 0.05, (
            f"PV efectiv + descarcare baterie ({effective_pv + discharge:.2f} kW) depaseste "
            f"limita invertorului ({inverter_kw} kW)"
        )
        assert effective_pv + discharge <= inverter_kw + 0.05
        if effective_pv < pv_raw - 0.05:
            curtailed_somewhere = True

    assert curtailed_somewhere, "PV-ul peste limita invertorului trebuie curtailat in cel putin un interval"


def test_infeasible_when_load_exceeds_all_available_power_sources(db):
    """Caz de infezabilitate REALA (independenta de banda SOC): fara PV, fara
    import de retea, consum peste puterea maxima de descarcare a bateriei --
    niciun set de decizii nu poate respecta bilantul energetic."""
    user = make_user(db, email="opt-realinfeasible@test.local")
    org = make_org(db, "Opt Real Infeasible Org")
    station = make_station(db, org, user, name="Opt Real Infeasible Station", grid_import_limit_kw=Decimal("0"))
    _add_tariffs(db, station)

    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    issued = utcnow()
    for h in range(40):
        t = start + timedelta(hours=h)
        db.add(
            PvForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(hours=1),
                source="test", predicted_power_kw=Decimal("0"), scenario="expected",
            )
        )
    for q in range(40 * 4):
        t = start + timedelta(minutes=15 * q)
        db.add(
            ConsumptionForecast(
                station_id=station.id, issued_at=issued, interval_start=t, interval_end=t + timedelta(minutes=15),
                source="test", base_load_kw=Decimal("10"), ev_component_kw=Decimal("0"),  # peste max_discharge (3 kW)
                flexible_component_kw=Decimal("0"), is_cold_start=True,
            )
        )
    db.commit()

    run = run_optimization_for_station(db, station.id, triggered_by="user")
    db.commit()

    assert run.is_fallback is True
    assert run.status == OptimizationRunStatus.infeasible.value

    plan = db.scalar(select(Plan).where(Plan.optimization_run_id == run.id))
    intervals = db.scalars(select(PlanInterval).where(PlanInterval.plan_id == plan.id)).all()
    assert all(float(pi.battery_power_target_kw) == 0 for pi in intervals), "planul de fallback trebuie sa fie de asteptare (baterie in hold)"
