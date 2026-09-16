from __future__ import annotations

from pathlib import Path


def test_dashboard_template_prioritizes_client_summary_before_advanced_charts():
    template = Path("app/web/templates/dashboard/station.html").read_text()

    assert "Pe scurt, ce se intampla acum" in template
    assert "Flux energetic actual" in template
    assert "Produce acum" in template
    assert "Consuma acum" in template
    assert 'id="advanced-dashboard-details"' in template
    assert "Analiza avansata: preturi, planuri si prognoze" in template
    assert template.index("Pe scurt, ce se intampla acum") < template.index("Analiza avansata")
    assert template.index("Flux energetic actual") < template.index("PV, consum, baterie, retea")


def test_dashboard_kpi_placeholders_are_explanatory_not_false_zeroes():
    template = Path("app/web/templates/dashboard/station.html").read_text()
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "Se incarca..." in template
    assert "fara date" in script
    assert "comparatie cu ${label}: indisponibila" in script
    assert "comparison_label" in script
