from pathlib import Path


def test_dashboard_forecast_charts_show_quality_badges():
    template = Path("app/web/templates/dashboard/station.html").read_text()
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert 'id="forecast-pv-quality"' in template
    assert 'id="forecast-load-quality"' in template
    assert "function updateForecastQuality(metric, points)" in script
    assert "forecast_confidence" in script
    assert "forecast_source" in script
    assert "date sintetice" in script
    assert "updateForecastQuality(metric, data);" in script
    assert "updateForecastQuality(metric, []);" in script
