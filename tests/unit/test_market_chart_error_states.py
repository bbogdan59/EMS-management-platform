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


def test_market_timeline_displays_resolution_metadata():
    template = Path("app/web/templates/market/prices.html").read_text()
    script = Path("app/web/static/js/market.js").read_text()

    assert 'id="chart-timeline-resolution"' in template
    assert "function describeTimelineResolution(payload)" in script
    assert '$("chart-timeline-resolution")' in script
    assert "payload.aggregation || {}" in script
    assert "payload.timezone" in script


def test_five_day_pzu_chart_can_switch_between_mwh_and_kwh():
    template = Path("app/web/templates/market/prices.html").read_text()
    script = Path("app/web/static/js/market.js").read_text()

    assert 'id="five-day-unit-label"' in template
    assert 'id="five-day-unit-toggles"' in template
    assert "const FIVE_DAY_PRICE_UNITS" in script
    assert '{ value: "mwh", label: "lei/MWh", factor: 1, decimals: 2 }' in script
    assert '{ value: "kwh", label: "lei/kWh", factor: 1 / 1000, decimals: 4 }' in script
    assert "let selectedFiveDayPriceUnit = \"mwh\";" in script
    assert "function convertFiveDayPrice(value, unitOption)" in script
    assert "Number(value) * unitOption.factor" in script
    assert "value: [p.aligned_t, convertFiveDayPrice(p.price_lei_mwh, unitOption)]" in script
    assert 'yAxis: { type: "value", name: unitOption.label }' in script
    assert "if (lastFiveDayOverlayPayload) renderFiveDayOverlay(lastFiveDayOverlayPayload);" in script
