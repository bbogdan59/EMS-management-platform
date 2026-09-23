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


def test_dashboard_today_month_kpis_group_related_metrics_visually():
    """Cerere client: import/export, incarcare/descarcare baterie si
    productie PV trebuie grupate vizual, nu intr-un grid plat de 6 casute
    fara nicio legatura intre ele. Reutilizeaza componenta existenta
    `field_group` (issue #48), aceeasi ca la gruparea campurilor de
    configurare -- fara elemente de UI noi, doar reorganizare."""
    template = Path("app/web/templates/dashboard/station.html").read_text()

    assert '{% from "partials/_field_group.html" import group as field_group %}' in template
    assert '{% call field_group("Productie / consum") %}' in template
    assert '{% call field_group("Retea: import / export") %}' in template
    assert '{% call field_group("Baterie: incarcare / descarcare") %}' in template
    # ID-urile folosite de JS raman neschimbate -- doar reorganizare vizuala.
    for suffix in ("pv", "load", "grid-import", "grid-export", "battery-charge", "battery-discharge"):
        assert f'id="kpi-{{{{ period_key }}}}-{suffix}"' in template
