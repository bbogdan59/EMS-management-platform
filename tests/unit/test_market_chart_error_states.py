from __future__ import annotations

from pathlib import Path


def test_market_charts_have_isolated_error_retry_states():
    template = Path("app/web/templates/market/prices.html").read_text()
    script = Path("app/web/static/js/market.js").read_text()

    assert template.count("error-state") >= 4
    assert template.count("retry-btn") >= 4
    assert "function showChartState(el, state)" in script
    assert "function wireRetry(el, loadFn)" in script
    assert script.count('showChartState(el, "error")') == 4
    for load_fn in ["loadTimeline", "loadYearlyOverlay", "loadForecast", "loadMonthly"]:
        assert f"wireRetry(el, {load_fn});" in script
