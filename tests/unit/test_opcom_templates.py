from __future__ import annotations

from pathlib import Path


def test_opcom_unchanged_import_status_is_rendered_as_ok():
    market_template = Path("app/web/templates/market/prices.html").read_text()
    operations_template = Path("app/web/templates/admin/operations.html").read_text()

    assert 'status.today.status in ("succeeded", "unchanged")' in market_template
    assert 'status.tomorrow.status in ("succeeded", "unchanged")' in market_template
    assert 'r.status == "unchanged"' in operations_template
    assert "neschimbat" in operations_template


def test_dashboard_and_admin_templates_do_not_duplicate_money_units():
    dashboard_template = Path("app/web/templates/dashboard/station.html").read_text()
    operations_template = Path("app/web/templates/admin/operations.html").read_text()

    assert "Cost efectiv import (lei/kWh)" not in dashboard_template
    assert "Venit efectiv export (lei/kWh)" not in dashboard_template
    assert "Cost efectiv import</p>" in dashboard_template
    assert "Venit efectiv export</p>" in dashboard_template
    assert "<th>Status</th><th>Cost net (lei)</th>" in operations_template
    assert "objective_value_lei | lei" not in operations_template


def test_opcom_ui_explains_mwh_and_kwh_price_units():
    market_template = Path("app/web/templates/market/prices.html").read_text()
    dashboard_template = Path("app/web/templates/dashboard/station.html").read_text()

    assert "Graficele de piata folosesc lei/MWh" in market_template
    assert "aceleasi preturi in lei/kWh" in market_template
    assert "Pret PZU (lei/MWh)" in market_template
    assert "Evolutie pret (lei/MWh)" in market_template
    assert "Suprapunere preturi pe ani (lei/MWh, zi-din-an)" in market_template
    assert "Predictie pret mediu zilnic (lei/MWh)" in market_template
    assert "Medii lunare pe ani (lei/MWh)" in market_template
    assert "Pret spot OPCOM PZU (lei/kWh, azi / maine)" in dashboard_template
    assert "convertit din OPCOM lei/MWh in lei/kWh" in dashboard_template
