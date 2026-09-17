from __future__ import annotations

from pathlib import Path


def test_market_charts_have_isolated_error_retry_states():
    template = Path("app/web/templates/market/prices.html").read_text()
    script = Path("app/web/static/js/market.js").read_text()

    assert template.count("error-state") >= 5
    assert template.count("retry-btn") >= 5
    assert "function showChartState(el, state)" in script
    assert "function wireRetry(el, loadFn)" in script
    assert script.count('showChartState(el, "error")') == 5
    for load_fn in ["loadTimeline", "loadFiveDayOverlay", "loadYearlyOverlay", "loadForecast", "loadMonthly"]:
        assert f"wireRetry(el, {load_fn});" in script


def test_market_chart_fetches_have_timeout_and_stale_request_cancellation():
    script = Path("app/web/static/js/market.js").read_text()

    assert "timeoutMs = 15000" in script
    assert "setTimeout(() => timeoutController.abort(), timeoutMs)" in script
    for controller in [
        "timelineController",
        "fiveDayOverlayController",
        "yearlyOverlayController",
        "forecastController",
        "monthlyController",
    ]:
        assert f"let {controller} = null;" in script
        assert f"if ({controller}) {controller}.abort();" in script
        assert f"controller !== {controller}" in script


def test_market_chart_fetches_cache_successful_payloads_briefly_by_url():
    script = Path("app/web/static/js/market.js").read_text()

    assert "const marketDataCache = new Map();" in script
    assert "const MARKET_DATA_CACHE_TTL_MS = 30000;" in script
    assert "const cached = marketDataCache.get(url);" in script
    assert "Date.now() - cached.ts < MARKET_DATA_CACHE_TTL_MS" in script
    assert "marketDataCache.set(url, { data, ts: Date.now() });" in script


def test_market_year_color_lookup_falls_back_for_unmatched_years():
    script = Path("app/web/static/js/market.js").read_text()

    guarded = "EMS_YEAR_COLORS[Math.max(availableYears.indexOf(Number(year)), 0) % EMS_YEAR_COLORS.length]"
    unguarded = "EMS_YEAR_COLORS[availableYears.indexOf(Number(year)) % EMS_YEAR_COLORS.length]"
    assert script.count(guarded) == 2
    assert unguarded not in script
