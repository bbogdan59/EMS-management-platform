import copy
import json
import os
from datetime import datetime, timedelta

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_health_diagnostics_ui import diagnostics_server  # noqa: F401
from tests.e2e.test_station_sun_map import _login


def projected(original):
    data = copy.deepcopy(original)
    now = datetime.fromisoformat(data["generated_at"])
    end = datetime.fromisoformat(data["horizon_end"])
    data.update(
        quality="estimated",
        confidence="nominal",
        energy_to_target_kwh="5",
        forecast_issued_at=now.isoformat(),
        flags=[],
    )
    data["soc"]["value"] = "50"
    data["power"]["value"] = "2"
    data["current_rate"] = {
        "status": "estimated",
        "reaches_target_at": (now + timedelta(hours=2.5)).isoformat(),
        "minutes_to_target": "150",
        "reason": None,
        "end_soc_percent": None,
        "peak_soc_percent": None,
    }
    data["solar"] = {
        "status": "estimated",
        "reaches_target_at": (now + timedelta(hours=2)).isoformat(),
        "minutes_to_target": "120",
        "reason": None,
        "end_soc_percent": "83",
        "peak_soc_percent": "100",
    }
    data["points"] = [
        {
            "at": now.isoformat(),
            "soc_percent": "50",
            "pv_kw": None,
            "load_kw": None,
            "battery_power_kw": "2",
            "kind": "observed",
        }
    ]
    at = now + timedelta(minutes=15)
    index = 1
    while at < end:
        data["points"].append(
            {
                "at": at.isoformat(),
                "soc_percent": str(min(100, 50 + index * 6.25)),
                "pv_kw": "4",
                "load_kw": "1",
                "battery_power_kw": "2.85",
                "kind": "estimated",
            }
        )
        at += timedelta(minutes=15)
        index += 1
    return data


def test_projection_dashboard_detail_refresh_responsive_and_errors(diagnostics_server):  # noqa: F811
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        page.clock.install()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        path = f"/api/v1/stations/{station_id}/battery-health/projection"
        original = page.request.get(base + path).json()
        page.goto(f"{base}/?station_id={station_id}")
        root = page.locator("[data-charge-projection]")
        expect(root.locator("[data-charge-content]")).to_be_visible()
        expect(root.locator("[data-charge-rate]")).to_have_text("Estimare indisponibila")
        data = projected(original)
        pattern = "**/battery-health/projection?*"
        page.route(
            pattern,
            lambda route: route.fulfill(content_type="application/json", body=json.dumps(data)),
        )
        page.reload()
        expect(root.locator("[data-charge-rate-note]")).to_contain_text("150 minute")
        root.locator("[data-charge-details]>summary").click()
        expect(root.locator("[data-charge-chart]")).to_have_attribute("data-chart-ready", "true")
        page.goto(f"{base}/stations/{station_id}/battery")
        expect(root.locator("[data-charge-solar-note]")).to_contain_text("120 minute")
        expect(root.locator("[data-charge-chart]")).to_have_attribute("data-chart-ready", "true")
        for width in (320, 390, 1440):
            page.set_viewport_size({"width": width, "height": 1050})
            root.scroll_into_view_if_needed()
            assert root.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            root.screenshot(path=f"/tmp/ems-charge-projection-{width}.png")
        page.evaluate(
            "document.documentElement.classList.add('dark'); document.dispatchEvent(new CustomEvent('ems:theme-change'))"
        )
        root.screenshot(path="/tmp/ems-charge-projection-dark.png")
        with page.expect_response(lambda response: "/battery-health/projection?" in response.url):
            page.clock.fast_forward(61000)
        page.unroute(pattern)
        page.route(pattern, lambda route: route.fulfill(status=503, body="{}"))
        with page.expect_response(lambda response: "/battery-health/projection?" in response.url):
            page.clock.fast_forward(61000)
        expect(root.locator("[data-charge-error]")).to_be_visible()
        expect(root.locator("[data-charge-content]")).to_be_hidden()
        expect(page.locator("#battery-content")).to_be_visible()
        page.unroute(pattern)
        root.locator("[data-charge-retry]").click()
        expect(root.locator("[data-charge-content]")).to_be_visible()
        expect(root.locator("[data-charge-rate-note]")).to_contain_text("Puterea de incarcare")
        assert not errors, errors
        browser.close()
