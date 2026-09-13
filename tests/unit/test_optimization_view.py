"""Teste unitare pure (fara DB/solver) pentru view-modelul explicabil de
optimizare (issue #47) -- `app/services/optimization_view.py`.

Folosim un obiect simplu (namedtuple-like `SimpleNamespace`) in loc de
`PlanInterval` ORM real, pentru ca `build_rows` accepta orice obiect cu
atributele relevante -- exact scopul separarii de DB/solver."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.optimization_view import build_rows, classify_action, group_segments


def _pi(
    start,
    *,
    battery_kw=0.0,
    grid_kw=0.0,
    soc_pct=50.0,
    pv_kw=0.0,
    load_kw=0.0,
    price_import=None,
    price_export=None,
    explanation=None,
    ev_kw=0.0,
):
    return SimpleNamespace(
        interval_start=start,
        interval_end=start + timedelta(minutes=15),
        pv_forecast_kw=pv_kw,
        load_forecast_kw=load_kw,
        battery_power_target_kw=battery_kw,
        grid_power_target_kw=grid_kw,
        battery_soc_target_percent=soc_pct,
        ev_charge_power_kw=ev_kw,
        price_import_lei_kwh=price_import,
        price_export_lei_kwh=price_export,
        explanation=explanation,
    )


def test_classify_action_covers_every_basic_case():
    assert classify_action(battery_power_target_kw=2.0, grid_power_target_kw=0.0) == "charge_pv"
    assert classify_action(battery_power_target_kw=2.0, grid_power_target_kw=1.0) == "charge_grid"
    assert classify_action(battery_power_target_kw=-2.0, grid_power_target_kw=0.0) == "discharge"
    assert classify_action(battery_power_target_kw=0.0, grid_power_target_kw=1.5) == "import"
    assert classify_action(battery_power_target_kw=0.0, grid_power_target_kw=-1.5) == "export"
    assert classify_action(battery_power_target_kw=0.0, grid_power_target_kw=0.0) == "hold"


def test_classify_action_ignores_noise_below_threshold():
    # Sub pragul de 0.01 kW, zgomotul numeric al solverului nu trebuie sa fie
    # raportat ca o "actiune" reala.
    assert classify_action(battery_power_target_kw=0.005, grid_power_target_kw=0.005) == "hold"


def test_build_rows_computes_interval_cost_with_sign_convention():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=2.0, price_import=1.0, price_export=0.3)],
        interval_hours=0.25,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.action_code == "import"
    # 2 kW import * 1.0 lei/kWh * 0.25h = 0.5 lei cost (pozitiv = cost, nu beneficiu)
    assert row.interval_cost_lei == 0.5


def test_build_rows_export_produces_negative_cost_ie_net_benefit():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=-4.0, price_import=1.0, price_export=0.3)],
        interval_hours=0.25,
    )
    row = rows[0]
    assert row.action_code == "export"
    # 4 kW export * 0.3 lei/kWh * 0.25h = 0.3 lei beneficiu -> cost = -0.3
    assert row.interval_cost_lei == -0.3


def test_build_rows_cost_is_none_when_no_price_available():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows([_pi(start, battery_kw=0.0, grid_kw=1.0, price_import=None, price_export=None)])
    assert rows[0].interval_cost_lei is None


def test_build_rows_does_not_treat_missing_required_price_as_zero():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    imported = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=1.0, price_import=None, price_export=0.4)]
    )
    exported = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=-1.0, price_import=1.0, price_export=None)]
    )
    idle = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=0.0, price_import=None, price_export=None)]
    )

    assert imported[0].interval_cost_lei is None
    assert exported[0].interval_cost_lei is None
    assert idle[0].interval_cost_lei == 0.0


def test_build_rows_reserve_band_reason_when_soc_at_floor():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=0.0, soc_pct=15.0)],
        min_reserve_soc_percent=15.0,
        max_normal_soc_percent=95.0,
    )
    assert rows[0].dominant_reason == "protejarea rezervei minime de SOC"


def test_build_rows_ceiling_reason_when_exporting_at_soc_cap():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows(
        [_pi(start, battery_kw=0.0, grid_kw=-1.0, soc_pct=95.0)],
        min_reserve_soc_percent=15.0,
        max_normal_soc_percent=95.0,
    )
    assert rows[0].dominant_reason == "plafonul SOC normal a fost atins (surplusul PV e exportat)"


def test_group_segments_merges_consecutive_identical_rows_and_splits_on_change():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    intervals = [
        _pi(start, battery_kw=1.0, grid_kw=0.0, soc_pct=50.0),
        _pi(start + timedelta(minutes=15), battery_kw=1.0, grid_kw=0.0, soc_pct=55.0),
        _pi(start + timedelta(minutes=30), battery_kw=0.0, grid_kw=1.0, soc_pct=55.0),
    ]
    rows = build_rows(intervals)
    segments = group_segments(rows)

    assert len(segments) == 2
    assert segments[0].action_code == "charge_pv"
    assert segments[0].interval_count == 2
    assert segments[0].start == start
    assert segments[0].end == start + timedelta(minutes=30)
    assert segments[1].action_code == "import"
    assert segments[1].interval_count == 1


def test_group_segments_splits_when_reason_changes_even_if_action_code_same():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    intervals = [
        # ambele "hold", dar unul e la rezerva minima (motiv diferit)
        _pi(start, battery_kw=0.0, grid_kw=0.0, soc_pct=15.0),
        _pi(start + timedelta(minutes=15), battery_kw=0.0, grid_kw=0.0, soc_pct=50.0),
    ]
    rows = build_rows(intervals, min_reserve_soc_percent=15.0, max_normal_soc_percent=95.0)
    segments = group_segments(rows)
    assert len(segments) == 2
    assert segments[0].dominant_reason == "protejarea rezervei minime de SOC"
    assert segments[1].dominant_reason == "echilibru cerere-oferta"


def test_group_segments_empty_input_returns_empty_list():
    assert group_segments([]) == []


def test_build_rows_preserves_stored_explanation_text():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows([_pi(start, explanation="Sistem in echilibru.")])
    assert rows[0].explanation == "Sistem in echilibru."


def test_segment_total_cost_sums_only_when_any_row_has_a_price():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    intervals = [
        _pi(start, battery_kw=0.0, grid_kw=2.0, price_import=1.0),
        _pi(start + timedelta(minutes=15), battery_kw=0.0, grid_kw=2.0, price_import=2.0),
    ]
    rows = build_rows(intervals, interval_hours=0.25)
    segments = group_segments(rows)
    assert len(segments) == 1
    # (2*1.0 + 2*2.0) * 0.25 = 1.5
    assert segments[0].total_cost_lei == 1.5


def test_segment_total_cost_is_unknown_when_one_interval_is_unknown():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = build_rows(
        [
            _pi(start, battery_kw=0.0, grid_kw=2.0, price_import=1.0),
            _pi(start + timedelta(minutes=15), battery_kw=0.0, grid_kw=2.0, price_import=None),
        ],
        interval_hours=0.25,
    )

    assert group_segments(rows)[0].total_cost_lei is None
