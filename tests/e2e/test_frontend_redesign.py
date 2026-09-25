from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta

from playwright.sync_api import expect, sync_playwright

pytest_plugins = ["tests.e2e.test_health_diagnostics_ui"]


def _emit(page, values, event="delta"):
    page.evaluate(
        "([event, values]) => window.testStream[event]({data: JSON.stringify({metrics: Object.entries(values).map(([metric, value]) => ({metric, value}))})})",
        [event, values],
    )


def _no_overflow(page):
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    if not page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"):
        page.screenshot(path="/tmp/ems-redesign-overflow.png", full_page=True)
        raise AssertionError(page.evaluate("[...document.querySelectorAll('main *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => [el.tagName, el.id, el.className, Math.round(el.getBoundingClientRect().right)]).slice(0,30)"))


def test_responsive_dashboard_metrics_navigation_and_charts(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # A controllable transport exercises real snapshot/delta rendering without
        # depending on Redis timing. All chart fixtures are explicitly simulated.
        page.add_init_script("""window.testStream = {};
            window.EventSource = class { addEventListener(name, callback) { window.testStream[name] = callback; } close() {} };
        """)
        page.goto(base + "/login")
        page.screenshot(path="/tmp/ems-redesign-login.png", full_page=True)
        page.fill("#email", "diagnostics-browser@test.local")
        page.fill("#password", "TestPass1234")
        page.click("button[type=submit]")
        page.goto(base + f"/?station_id={station_id}")
        expect(page.locator("#kpi-pv")).to_have_text("1,00 kW")
        expect(page.locator("#kpi-load")).to_have_text("fara date")
        _emit(page, {"ev_connected": True, "ev_power_kw": 0, "grid_power_kw": -2.8, "battery_soc_percent": 0})
        expect(page.locator("#kpi-ev")).to_have_text("0,00 kW")
        expect(page.locator("#kpi-soc")).to_have_text("0,0 %")
        expect(page.locator("#grid-direction-note")).to_contain_text("Export")
        _emit(page, {"grid_power_kw": 2.8, "battery_power_kw": -1.1})
        expect(page.locator("#grid-direction-note")).to_contain_text("Import")
        expect(page.locator("#battery-direction-note")).to_contain_text("descarcare")
        _emit(page, {"ev_power_kw": None, "battery_soc_percent": None, "data_quality": "stale"})
        expect(page.locator("#kpi-ev")).to_have_text("fara date")
        expect(page.locator("#kpi-soc")).to_have_text("fara date")
        expect(page.locator("#kpi-quality")).to_have_text("invechit")
        expect(page.locator("#energy-flow-diagram")).to_have_css("filter", "grayscale(1)")

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        points = []
        for step in range(96):
            pv = max(0, math.sin((step - 25) / 48 * math.pi)) * 6
            load = 1.8 + math.sin(step * 0.7) * 0.4
            points.append({"t": (now - timedelta(hours=24) + timedelta(minutes=15 * step)).isoformat(), "pv_kw": pv, "load_kw": load, "battery_kw": (pv - load) * 0.35, "grid_kw": (load - pv) * 0.65, "soc_pct": 52 + pv * 5, "data_quality": "simulated"})
        page.route("**/data/timeseries?*", lambda route: route.fulfill(json={"points": points, "resolution": "15m", "coverage": 0.98, "aggregation": {"pv_kw": "medie"}}))
        metrics = {metric: {"value": value, "quality": "simulated", "coverage": 0.98, "comparison": {"delta": 1.1, "delta_percent": 8.2}} for metric, value in zip(["pv", "load", "grid_import", "grid_export", "battery_charge", "battery_discharge"], [24.85, 18.32, 3.14, 6.72, 5.28, 2.81], strict=True)}
        page.route("**/data/energy-kpis", lambda route: route.fulfill(json={period: {"metrics": metrics, "comparison_label": "ieri"} for period in ("today", "month")}))
        page.reload()
        _emit(page, {"pv_power_kw": 6, "load_power_kw": 2.1, "battery_power_kw": 1.1, "battery_soc_percent": 83, "grid_power_kw": -2.8, "ev_connected": True, "ev_power_kw": 0, "price_buy_lei_kwh": 0.89085, "price_sell_lei_kwh": 0.00085, "data_quality": "simulated"})
        expect(page.locator("#kpi-today-pv")).to_have_text("24,85 kWh")
        expect(page.locator("#kpi-price-sell")).to_have_text("0,00085 lei/kWh")
        page.locator("#chart-power").scroll_into_view_if_needed()
        expect(page.locator("#chart-power")).to_have_attribute("data-chart-ready", "true")
        page.evaluate("document.fonts.ready")
        page.evaluate("window.scrollTo(0,0)")
        _no_overflow(page)
        page.screenshot(path="/tmp/ems-redesign-desktop.png", full_page=True)
        page.get_by_role("button", name="Comuta tema").click()
        expect(page.locator("html")).to_have_class("h-full dark")
        page.locator("#range-select").select_option("7d")
        expect(page.locator("#export-link")).to_have_attribute("href", f"/stations/{station_id}/export.csv?range=7d")
        page.wait_for_function("echarts.getInstanceByDom(document.getElementById('chart-power')).getOption().xAxis[0].axisLabel.color === '#afc0a7'")
        page.screenshot(path="/tmp/ems-redesign-dark.png", full_page=True)
        page.get_by_role("button", name="Comuta tema").click()

        for width in (390, 320, 768):
            page.set_viewport_size({"width": width, "height": 844})
            _no_overflow(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _emit(page, {"pv_power_kw": 12345.67})
        expect(page.locator("#kpi-pv")).to_have_text("12.345,67 kW")
        assert page.locator("#kpi-pv").evaluate("el => el.scrollWidth <= el.clientWidth")
        _emit(page, {"pv_power_kw": 6})
        page.screenshot(path="/tmp/ems-redesign-mobile.png", full_page=True)
        page.screenshot(path="/tmp/ems-redesign-phone.png")
        opener = page.locator('.workspace-topbar [data-menu-toggle]')
        opener.click()
        expect(page.get_by_role("dialog")).to_be_visible()
        assert page.locator("#app-workspace").evaluate("el => el.inert")
        page.keyboard.press("Shift+Tab")
        assert page.locator("#app-sidebar").evaluate("el => el.contains(document.activeElement)")
        page.keyboard.press("Escape")
        expect(opener).to_be_focused()
        assert page.locator("#app-sidebar").evaluate("el => el.inert")
        assert not page.locator("#app-workspace").evaluate("el => el.inert")
        for route in ("ev", "control", "recommendations", "config", "preferences", "tariffs", "devices", "health"):
            page.goto(base + f"/stations/{station_id}/{route}")
            expect(page.locator("h1")).to_be_visible()
            _no_overflow(page)
        page.goto(base + f"/market/prices?station_id={station_id}")
        _no_overflow(page)
        page.screenshot(path="/tmp/ems-redesign-market-mobile.png", full_page=True)
        assert errors == []
        browser.close()
