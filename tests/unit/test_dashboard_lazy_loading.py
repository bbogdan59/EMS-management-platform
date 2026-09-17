from __future__ import annotations

from pathlib import Path


def test_dashboard_charts_are_registered_for_lazy_visibility_loading():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "function lazyLoadWidget(chartElId, loadFn)" in dashboard_js
    assert "new IntersectionObserver" in dashboard_js
    assert "rootMargin: \"160px 0px\"" in dashboard_js

    for chart_id in [
        "chart-power",
        "chart-soc",
        "chart-prices",
        "chart-plan",
        "chart-forecast-pv",
        "chart-forecast-load",
        "chart-heatmap",
        "chart-energy-daily",
        "chart-energy-monthly",
    ]:
        assert f'lazyLoadWidget("{chart_id}"' in dashboard_js


def test_technical_dashboard_charts_are_progressively_disclosed():
    template = Path("app/web/templates/dashboard/station.html").read_text()

    assert 'id="advanced-dashboard-details"' in template
    assert "Plan incarcare/descarcare" in template
    assert "Prognoza vs. realizat - PV" in template
    assert template.index('id="advanced-dashboard-details"') < template.index("Plan incarcare/descarcare")


def test_dashboard_timeseries_charts_mark_quality_intervals():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "function qualityMarkAreas(points)" in dashboard_js
    assert "data_quality" in dashboard_js
    assert "markArea" in dashboard_js
    assert "markAreas: qualityMarkAreas(points)" in dashboard_js


def test_dashboard_timeseries_line_symbols_are_threshold_based():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "const EMS_CHART_SYMBOL_THRESHOLD = 48" in dashboard_js
    assert "item.data.length <= EMS_CHART_SYMBOL_THRESHOLD" in dashboard_js
    assert 'const mk = (key, name) => ({ name, type: "line", data:' in dashboard_js


def test_daily_export_energy_chart_preserves_missing_values_as_gaps():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "d.grid_export_kwh == null ? null : -d.grid_export_kwh" in dashboard_js
    assert "data: daily.map((d) => -d.grid_export_kwh)" not in dashboard_js
