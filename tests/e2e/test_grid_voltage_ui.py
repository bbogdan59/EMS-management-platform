import os
from datetime import date, timedelta

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_health_diagnostics_ui import diagnostics_server  # noqa: F401
from tests.e2e.test_station_sun_map import _login


def test_voltage_daily_chart_mobile_theme_dates_and_retry(diagnostics_server):  # noqa: F811
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.clock.install()
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        _login(page, base)
        page.goto(f"{base}/?station_id={station_id}")
        panel = page.locator("[data-grid-voltage]")
        panel.scroll_into_view_if_needed()
        expect(panel.locator("[data-voltage-chart]")).to_have_attribute("data-chart-ready", "true")
        expect(panel.locator(".voltage-phase").first).to_contain_text("262,1")
        expect(panel.locator(".voltage-phase").nth(1)).to_contain_text("Faza fara citiri")
        panel.locator("summary").click()
        expect(panel.locator("tbody tr")).to_have_count(1)
        expect(panel.locator("tbody")).to_contain_text("GMT")
        panel.locator("summary").click()
        for width in (320, 390, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            panel.scroll_into_view_if_needed()
            expect(panel.locator("[data-voltage-content]")).to_be_visible()
            assert panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            if width == 320:
                panel.screenshot(path="/tmp/ems-voltage-mobile.png")
        page.get_by_role("button", name="Comuta tema").click()
        panel.screenshot(path="/tmp/ems-voltage-dark.png")
        page.get_by_role("button", name="Comuta tema").click()
        panel.screenshot(path="/tmp/ems-voltage-light.png")
        today = panel.locator("[data-voltage-day]").input_value()
        previous = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        panel.locator("[data-voltage-day]").fill(previous)
        expect(panel.locator("[data-voltage-empty]")).to_be_visible()
        expect(panel.locator(".voltage-live")).to_have_text("Istoric zilnic")
        panel.get_by_role("button", name="Astazi", exact=True).click()
        expect(panel.locator(".voltage-phase").first).to_contain_text("262,1")
        pattern = f"**/api/v1/stations/{station_id}/grid-voltage*"
        page.route(pattern, lambda route: route.fulfill(status=503, body="unavailable"))
        panel.get_by_role("button", name="Astazi", exact=True).click()
        expect(panel.locator("[data-voltage-error]")).to_be_visible()
        expect(panel.locator("[data-voltage-content]")).to_be_hidden()
        page.unroute(pattern)
        panel.get_by_role("button", name="Reincearca", exact=True).click()
        expect(panel.locator(".voltage-phase").first).to_contain_text("262,1")
        expect(panel.locator("[data-voltage-error]")).to_be_hidden()
        with page.expect_response(lambda response: "/grid-voltage?" in response.url) as refreshed:
            page.clock.fast_forward(61000)
        assert refreshed.value.status == 200
        assert not failures, failures
        browser.close()
