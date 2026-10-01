from __future__ import annotations

from pathlib import Path


def test_dashboard_template_prioritizes_client_summary_before_advanced_charts():
    template = Path("app/web/templates/dashboard/station.html").read_text()

    assert "Pe scurt, ce se intampla acum" in template
    assert "Flux energetic actual" in template
    assert "Produce acum" in template
    assert "Consuma acum" in template
    first_sheet = template.index("{% call sheet(")
    assert template.index("Pe scurt, ce se intampla acum") < first_sheet
    assert template.index("Flux energetic actual") < first_sheet < template.index("PV, consum, baterie, retea")


def _sheet(template: str, key: str) -> str:
    start = template.index(f"{{% call sheet('{key}'")
    return template[start:template.index("{% endcall %}", start)]


def test_dashboard_overview_is_tiles_and_every_detail_lives_in_a_sheet():
    """Cerere client: primul ecran contine doar fluxul live si widget-uri mici
    pe categorii; graficele si tabelele se deschid in panouri de detalii."""
    template = Path("app/web/templates/dashboard/station.html").read_text()
    overview = template[template.index("{% block content %}"):template.index("{% call sheet(")]

    for key in ("solar", "consum", "baterie", "retea", "energie", "bani", "control", "casa"):
        assert overview.count(f'data-sheet-open="sheet-{key}"') == 1
        assert f"{{% call sheet('{key}'" in template
    assert 'id="chart-' not in overview
    assert "<table" not in overview and "<details" not in overview
    assert overview.count("{% include") == 1 and "dashboard/_flow_diagram.html" in overview


def test_dashboard_money_is_grouped_in_one_tile_and_one_sheet():
    """Cerere client: valorile in lei erau raspandite in trei zone ale paginii."""
    template = Path("app/web/templates/dashboard/station.html").read_text()
    money = _sheet(template, "bani")

    for element_id in (
        "kpi-price-buy", "kpi-price-sell", "kpi-savings", "kpi-ems-benefit",
        "kpi-gross-pv-value", "kpi-self-consumption", "kpi-export-revenue", "chart-prices",
    ):
        assert template.count(f'id="{element_id}"') == 1
        assert f'id="{element_id}"' in money
    outside = template.replace(money, "")
    assert outside.count("lei") == 0


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
