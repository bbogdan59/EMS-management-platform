from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.dashboard_service import _energy_kpi_value


def test_energy_kpi_value_keeps_zero_distinct_from_missing():
    row = SimpleNamespace(
        pv_energy_kwh=Decimal("0.0000"),
        load_energy_kwh=None,
        coverage={"pv": 1.0, "load": 0.0},
        data_quality="measured",
    )

    assert _energy_kpi_value(row, "pv_energy_kwh", "pv") == {
        "value": 0.0,
        "coverage": 1.0,
        "quality": "measured",
    }
    assert _energy_kpi_value(row, "load_energy_kwh", "load") == {
        "value": None,
        "coverage": 0.0,
        "quality": "missing",
    }


def test_energy_kpi_value_marks_partial_coverage_without_dropping_value():
    row = SimpleNamespace(
        pv_energy_kwh=Decimal("12.5000"),
        coverage={"pv": 0.5},
        data_quality="measured",
    )

    assert _energy_kpi_value(row, "pv_energy_kwh", "pv") == {
        "value": 12.5,
        "coverage": 0.5,
        "quality": "partial",
    }
