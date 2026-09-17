from __future__ import annotations

from pathlib import Path


def test_dashboard_charts_are_registered_for_lazy_visibility_loading():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "function lazyLoadWidget(chartElId, loadFn)" in dashboard_js
    assert "new IntersectionObserver" in dashboard_js
    assert "rootMargin: \"160px 0px\"" in dashboard_js

    for chart_id in [
        "chart-power",
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


def test_dashboard_power_chart_overlays_faded_soc_on_left_axis():
    template = Path("app/web/templates/dashboard/station.html").read_text()
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "PV, consum, baterie, retea + SOC baterie" in template
    assert 'id="chart-soc"' not in template
    assert 'lazyLoadWidget("chart-soc"' not in dashboard_js
    assert 'name: "SOC baterie"' in dashboard_js
    assert "yAxisIndex: 0" in dashboard_js
    assert 'yAxis: [\n          { type: "value", name: "%", min: 0, max: 100 },\n          { type: "value", name: "kW" },\n        ]' in dashboard_js
    assert "lineStyle: { width: 1.5, opacity: 0.38 }" in dashboard_js
    assert "areaStyle: { opacity: 0.04 }" in dashboard_js


def test_dashboard_heatmap_uses_15m_slots_with_hour_labels_and_kw_values():
    template = Path("app/web/templates/dashboard/station.html").read_text()
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "Heatmap consum (15 minute x zi din saptamana)" in template
    assert "Insuficiente agregate la 15 minute pentru heatmap." in template
    assert "const slots = [...Array(96).keys()].map((slot) => {" in dashboard_js
    assert "data.map((d) => [d.slot, d.weekday, Number(d.avg_load_kw.toFixed(3))])" in dashboard_js
    assert 'axisLabel: { formatter: (value, index) => (index % 4 === 0 ? value.slice(0, 2) : "") }' in dashboard_js
    assert "${params.value[2]} kW" in dashboard_js


def test_dashboard_timeseries_line_symbols_are_threshold_based():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "const EMS_CHART_SYMBOL_THRESHOLD = 48" in dashboard_js
    assert "item.data.length <= EMS_CHART_SYMBOL_THRESHOLD" in dashboard_js
    assert 'const mk = (key, name) => ({ name, type: "line", yAxisIndex: 1, data:' in dashboard_js


def test_daily_export_energy_chart_preserves_missing_values_as_gaps():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()

    assert "d.grid_export_kwh == null ? null : -d.grid_export_kwh" in dashboard_js
    assert "data: daily.map((d) => -d.grid_export_kwh)" not in dashboard_js
