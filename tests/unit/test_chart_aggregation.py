from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import chart_aggregation as agg


def _t(minute, second=0):
    return datetime(2026, 1, 1, 10, minute, second, tzinfo=UTC)


# --- choose_resolution ----------------------------------------------------


@pytest.mark.parametrize(
    "range_key,expected",
    [
        ("24h", "15m"),
        ("7d", "30m"),
        ("30d", "1h"),
        ("1y", "1d"),
        ("unknown-range", "15m"),  # necunoscut -> cea mai detaliata, nu bruta nemarginita
    ],
)
def test_choose_resolution_matches_contract(range_key, expected):
    assert agg.choose_resolution(range_key) == expected


# --- aggregate_series: metric-aware (power vs energy vs SOC) --------------


def test_power_metrics_are_averaged_not_summed():
    """Puterea instantanee (kW) NU trebuie insumata pe bucket -- ar produce
    valori absurde (ex. 4x mai mari la 4 esantioane/bucket)."""
    rows = [
        {"t": _t(0), "pv_kw": 2.0},
        {"t": _t(5), "pv_kw": 4.0},
        {"t": _t(10), "pv_kw": 6.0},
    ]
    out = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"pv_kw": "mean"})
    assert len(out) == 1
    assert out[0]["pv_kw"] == pytest.approx(4.0)  # medie (2+4+6)/3, nu suma (12)


def test_energy_metric_can_be_summed():
    """Energia (kWh, aditiva in timp) e singura pentru care suma pe bucket
    are sens fizic."""
    rows = [
        {"t": _t(0), "pv_energy_kwh": 0.25},
        {"t": _t(5), "pv_energy_kwh": 0.25},
        {"t": _t(10), "pv_energy_kwh": 0.25},
    ]
    out = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"pv_energy_kwh": "sum"})
    assert out[0]["pv_energy_kwh"] == pytest.approx(0.75)


def test_soc_percentage_refuses_to_be_summed():
    """Bug-ul specific pe care contractul de agregare trebuie sa il previna:
    SOC-ul (procent) NU se insumeaza niciodata -- functia refuza explicit,
    nu doar "din obisnuinta" in apelanti."""
    rows = [{"t": _t(0), "soc_pct": 50.0}, {"t": _t(5), "soc_pct": 52.0}]
    with pytest.raises(ValueError, match="procent/SOC"):
        agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"soc_pct": "sum"})


@pytest.mark.parametrize("metric_name", ["soc_pct", "battery_soc_percent", "load_pct", "utilization_percent"])
def test_percentage_like_names_are_all_refused_for_sum(metric_name):
    rows = [{"t": _t(0), metric_name: 10.0}]
    with pytest.raises(ValueError):
        agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={metric_name: "sum"})


def test_soc_percentage_can_still_be_averaged():
    rows = [{"t": _t(0), "soc_pct": 50.0}, {"t": _t(5), "soc_pct": 60.0}]
    out = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"soc_pct": "mean"})
    assert out[0]["soc_pct"] == pytest.approx(55.0)


def test_min_and_max_aggregation_methods():
    rows = [{"t": _t(0), "price": 1.0}, {"t": _t(5), "price": 3.0}, {"t": _t(10), "price": 2.0}]
    out_min = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"price": "min"})
    out_max = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"price": "max"})
    assert out_min[0]["price"] == pytest.approx(1.0)
    assert out_max[0]["price"] == pytest.approx(3.0)


# --- bucketing correctness -------------------------------------------------


def test_points_are_split_across_multiple_buckets():
    rows = [
        {"t": _t(0), "pv_kw": 1.0},
        {"t": _t(10), "pv_kw": 3.0},
        {"t": _t(20), "pv_kw": 5.0},  # bucket urmator (>= 15 min dupa primul)
    ]
    out = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"pv_kw": "mean"})
    assert len(out) == 2
    assert out[0]["pv_kw"] == pytest.approx(2.0)  # medie (1+3)/2
    assert out[1]["pv_kw"] == pytest.approx(5.0)


def test_missing_data_stays_none_never_becomes_zero():
    """Un bucket fara nicio valoare non-null pentru o metrica NU devine 0 --
    ramane `None`, ca sa nu para o citire reala de zero."""
    rows = [{"t": _t(0), "pv_kw": 2.0, "grid_kw": None}]
    out = agg.aggregate_series(rows, timestamp_key="t", bucket_seconds=900, metrics={"pv_kw": "mean", "grid_kw": "mean"})
    assert out[0]["pv_kw"] == pytest.approx(2.0)
    assert out[0]["grid_kw"] is None


def test_empty_input_produces_empty_output_not_a_zero_series():
    out = agg.aggregate_series([], timestamp_key="t", bucket_seconds=900, metrics={"pv_kw": "mean"})
    assert out == []


# --- coverage ---------------------------------------------------------------


def test_coverage_is_fraction_of_buckets_with_at_least_one_point():
    start = _t(0)
    end = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)  # 4 bucket-uri de 15 min
    rows = [{"t": _t(0)}, {"t": _t(30)}]  # doar bucket-urile 0 si 2 au date
    coverage = agg.compute_coverage(rows, timestamp_key="t", start=start, end=end, bucket_seconds=900)
    assert coverage == pytest.approx(0.5)


def test_coverage_counts_partial_bucket_at_range_end():
    start = _t(0)
    end = datetime(2026, 1, 1, 10, 16, 0, tzinfo=UTC)  # atinge bucket-urile 10:00 si 10:15
    rows = [{"t": _t(0)}]
    coverage = agg.compute_coverage(rows, timestamp_key="t", start=start, end=end, bucket_seconds=900)
    assert coverage == pytest.approx(0.5)


def test_coverage_is_zero_for_no_rows():
    start = _t(0)
    end = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)
    coverage = agg.compute_coverage([], timestamp_key="t", start=start, end=end, bucket_seconds=900)
    assert coverage == 0.0


def test_coverage_never_exceeds_one_even_with_dense_raw_data():
    start = _t(0)
    end = datetime(2026, 1, 1, 10, 15, 0, tzinfo=UTC)  # 1 bucket asteptat
    rows = [{"t": _t(0, s)} for s in range(0, 60, 5)]  # multe puncte in ACELASI bucket
    coverage = agg.compute_coverage(rows, timestamp_key="t", start=start, end=end, bucket_seconds=900)
    assert coverage == 1.0


def test_coverage_ignores_points_outside_requested_range():
    start = _t(0)
    end = datetime(2026, 1, 1, 10, 30, 0, tzinfo=UTC)  # 2 bucket-uri asteptate
    rows = [
        {"t": datetime(2026, 1, 1, 9, 45, tzinfo=UTC)},
        {"t": _t(0)},
        {"t": datetime(2026, 1, 1, 10, 30, tzinfo=UTC)},
        {"t": datetime(2026, 1, 1, 11, 0, tzinfo=UTC)},
    ]

    coverage = agg.compute_coverage(rows, timestamp_key="t", start=start, end=end, bucket_seconds=900)

    assert coverage == pytest.approx(0.5)


def test_coverage_ignores_points_outside_requested_half_open_range():
    start = _t(0)
    end = datetime(2026, 1, 1, 10, 15, 0, tzinfo=UTC)
    rows = [{"t": _t(15)}, {"t": _t(30)}]
    coverage = agg.compute_coverage(rows, timestamp_key="t", start=start, end=end, bucket_seconds=900)
    assert coverage == 0.0
