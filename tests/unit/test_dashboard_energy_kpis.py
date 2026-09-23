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
        "comparison": None,
    }
    assert _energy_kpi_value(row, "load_energy_kwh", "load") == {
        "value": None,
        "coverage": 0.0,
        "quality": "missing",
        "comparison": None,
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
        "comparison": None,
    }


def test_energy_kpi_value_adds_comparison_only_with_comparable_elapsed_coverage():
    """`current_elapsed_coverage` (acoperirea PORTIUNII DEJA SCURSE din
    perioada curenta), NU `row.coverage` (relativa la perioada INTREAGA),
    e cea care decide daca se arata comparatia -- vezi regresia din
    `test_dashboard_service.py::test_energy_kpi_today_comparison_uses_elapsed_window_not_full_previous_day`
    pentru simptomul concret pe care aceasta distinctie il repara."""
    row = SimpleNamespace(
        pv_energy_kwh=Decimal("15.0000"),
        coverage={"pv": 1.0},
        data_quality="measured",
    )
    comparison_totals = {"pv_energy_kwh": Decimal("10.0000")}

    assert _energy_kpi_value(
        row, "pv_energy_kwh", "pv",
        current_elapsed_coverage={"pv": 1.0},
        comparison_totals=comparison_totals,
        comparison_coverage={"pv": 1.0},
    )["comparison"] == {
        "previous_value": 10.0,
        "delta": 5.0,
        "delta_percent": 50.0,
    }

    # Perioada curenta nu are inca destule date in portiunea scursa.
    assert _energy_kpi_value(
        row, "pv_energy_kwh", "pv",
        current_elapsed_coverage={"pv": 0.2},
        comparison_totals=comparison_totals,
        comparison_coverage={"pv": 1.0},
    )["comparison"] is None

    # Perioada curenta e ok, dar fereastra anterioara comparabila nu are
    # destule date.
    assert _energy_kpi_value(
        row, "pv_energy_kwh", "pv",
        current_elapsed_coverage={"pv": 1.0},
        comparison_totals=comparison_totals,
        comparison_coverage={"pv": 0.2},
    )["comparison"] is None

    # Fara argumente de comparatie deloc (echivalentul vechiului
    # `previous_row=None`) -- comparatia ramane indisponibila, nu eroare.
    assert _energy_kpi_value(row, "pv_energy_kwh", "pv")["comparison"] is None
