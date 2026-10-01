from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

from playwright.sync_api import expect, sync_playwright

from app.services.sun_map_service import sun_map

pytest_plugins = ["tests.e2e.test_health_diagnostics_ui"]

TILE = '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#e7ebe3"/><path d="M0 112H256M112 0V256" stroke="#d0d4ce" stroke-width="24"/><g fill="#fff" stroke="#bac1b7"><rect x="22" y="20" width="65" height="63"/><rect x="154" y="22" width="76" height="55"/><rect x="24" y="151" width="55" height="78"/><rect x="155" y="162" width="69" height="65"/></g></svg>'


def _login(page, base):
    page.route("https://tile.openstreetmap.org/**", lambda route: route.fulfill(content_type="image/svg+xml", body=TILE))
    page.goto(base + "/login")
    page.fill("#email", "diagnostics-browser@test.local")
    page.fill("#password", "TestPass1234")
    page.click("button[type=submit]")


def test_live_sun_map_refresh_night_error_and_responsive_layout(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        payload = sun_map(44.43, 26.1, "Europe/Bucharest", datetime(2026, 6, 21, 7, tzinfo=UTC))
        failing = False

        def response(route):
            if failing:
                route.fulfill(status=503, body="Unavailable")
            else:
                route.fulfill(json=payload)

        page.route("**/data/sun", response)
        page.clock.install()
        page.goto(base + f"/?station_id={station_id}")
        card = page.locator("[data-sun-map]")
        expect(card).to_be_hidden()
        page.get_by_role("button", name="Detalii productie solara").click()
        expect(card).to_have_attribute("data-sun-ready", "true")
        expect(page.locator("[data-sun-clock]")).to_have_text("10:00")
        expect(page.locator(".sun-day-path")).to_have_attribute("d", re.compile("M.+L"))
        expect(page.locator(".sun-current")).to_be_visible()
        assert "grayscale(1)" in page.locator(".leaflet-tile-pane").evaluate("el => getComputedStyle(el).filter")
        expect(card.locator(".leaflet-control-zoom")).to_have_count(0)
        marker_position = page.locator(".house-map-pin").get_attribute("style")
        canvas = card.locator("[data-map-canvas]")
        bounds = canvas.bounding_box()
        page.mouse.move(bounds["x"] + 50, bounds["y"] + 80)
        page.mouse.down()
        page.mouse.move(bounds["x"] + 120, bounds["y"] + 130, steps=8)
        page.mouse.up()
        canvas.dblclick(position={"x": 60, "y": 100})
        assert page.locator(".house-map-pin").get_attribute("style") == marker_position
        page.mouse.wheel(0, 220)
        page.wait_for_function("document.querySelector('#sheet-solar .ov-sheet-body').scrollTop > 0")
        assert page.locator(".house-map-pin").get_attribute("style") == marker_position
        first_x = page.locator(".sun-current .sun-disc").get_attribute("cx")
        payload = sun_map(44.43, 26.1, "Europe/Bucharest", datetime(2026, 6, 21, 16, tzinfo=UTC))
        page.clock.fast_forward(31000)
        expect(page.locator("[data-sun-clock]")).to_have_text("19:00")
        assert page.locator(".sun-current .sun-disc").get_attribute("cx") != first_x
        card.screenshot(path="/tmp/ems-sun-map-desktop.png")
        page.evaluate("emsToggleTheme()")  # the top bar is inert behind a modal sheet
        card.screenshot(path="/tmp/ems-sun-map-dark.png")
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            card.scroll_into_view_if_needed()
            page.wait_for_function("""() => {
                const map = document.querySelector('[data-map-canvas]').getBoundingClientRect();
                const pin = document.querySelector('.house-map-pin').getBoundingClientRect();
                return Math.abs(pin.x + pin.width / 2 - map.x - map.width / 2) < 2;
            }""")
            expect(page.locator(".sun-current")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        card.screenshot(path="/tmp/ems-sun-map-mobile.png")

        payload = sun_map(44.43, 26.1, "Europe/Bucharest", datetime(2026, 6, 21, 22, tzinfo=UTC))
        page.clock.fast_forward(31000)
        expect(page.locator(".sun-current.is-night")).to_be_visible()
        expect(page.locator("[data-sun-date]")).to_contain_text("22 iunie")
        expect(page.locator("[data-sun-condition]")).to_contain_text("sub orizont")
        failing = True
        page.clock.fast_forward(31000)
        expect(page.locator("[data-sun-status]")).to_have_text("Actualizare indisponibila")
        expect(page.locator(".sun-current")).to_have_count(0)
        failing = False
        page.clock.fast_forward(31000)
        expect(page.locator(".sun-current")).to_be_visible()
        payload = sun_map(None, None, "Europe/Bucharest", datetime(2026, 6, 21, 22, tzinfo=UTC))
        page.clock.fast_forward(31000)
        expect(page.locator("[data-sun-status]")).to_have_text("Locatie lipsa")
        expect(page.locator(".house-map-pin")).to_have_count(0)
        assert not errors
        browser.close()


def test_dashboard_history_preserves_gaps_zero_export_and_provenance(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 1440, "height": 1200}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        now = now.replace(minute=now.minute // 15 * 15)
        points = [
            {"t": (now - timedelta(minutes=15 * step)).isoformat(), "pv_kw": pv, "load_kw": None,
             "soc_pct": soc, "grid_kw": grid, "data_quality": quality}
            for step, pv, soc, grid, quality in [
                (8, 1, 0, -2, "simulated"), (7, None, None, 0, "measured"),
                (4, 4, 35, -1, "stale"), (3, 2, 50, 2, "estimated"), (1, 3, 60, 0, "measured"),
            ]
        ]
        payload = {"points": points, "timezone": "Europe/Bucharest", "resolution": "15m", "coverage": 0.8}
        failing = False

        def history_response(route):
            if failing:
                route.fulfill(status=503, body="Unavailable")
            else:
                route.fulfill(json=payload)

        page.route("**/data/timeseries?range=24h", history_response)
        page.clock.install()
        page.goto(base + f"/?station_id={station_id}")
        expect(page.locator("#history-pv")).to_have_attribute("data-chart-ready", "true")
        expect(page.locator('[data-history="load"] .history-empty')).to_have_text("Fara date istorice")
        expect(page.locator("#history-load")).to_be_hidden()
        expect(page.locator('[data-history="pv"] [data-history-status]')).to_contain_text("simulat")
        expect(page.locator('[data-history="pv"] [data-history-status]')).to_contain_text("intarziat")
        expect(page.locator('[data-history="pv"] [data-history-status]')).to_contain_text("estimat")
        values = page.evaluate("""() => Object.fromEntries(['pv', 'soc', 'grid'].map(key => [key,
            echarts.getInstanceByDom(document.getElementById('history-' + key)).getOption().series[0].data.map(point => point.value[1])]))""")
        assert 0 in values["soc"]
        assert -2 in values["grid"] and 0 in values["grid"] and 2 in values["grid"]
        assert values["pv"].count(None) == 3
        flow = page.locator(".flow-panel").bounding_box()
        live = page.locator(".overview-metrics").bounding_box()
        tiles = page.locator(".ov-grid").bounding_box()
        assert abs(flow["y"] - live["y"]) < 2
        assert live["x"] > flow["x"] + flow["width"]
        assert tiles["y"] >= max(flow["y"] + flow["height"], live["y"] + live["height"])
        assert tiles["y"] + tiles["height"] < 1200, "overview must fit one desktop screen"
        page.screenshot(path="/tmp/ems-dashboard-sage-desktop.png", full_page=True)
        page.get_by_role("button", name="Comuta tema").click()
        page.screenshot(path="/tmp/ems-dashboard-sage-dark.png", full_page=True)
        assert page.evaluate("""() => ['pv', 'soc', 'grid'].every(key =>
            echarts.getInstanceByDom(document.getElementById('history-' + key)).getOption().legend.every(legend => legend.show === false))""")
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("""() => ({
                width: innerWidth, scroll: document.documentElement.scrollWidth,
                elements: [...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1)
                    .map(el => [el.tagName, el.id, String(el.className), el.getBoundingClientRect().right, getComputedStyle(el).position]).slice(0, 30)
            })""")
        page.screenshot(path="/tmp/ems-dashboard-sage-mobile.png", full_page=True)
        failing = True
        page.clock.fast_forward(61000)
        expect(page.locator('[data-history="pv"] .history-empty')).to_have_text("Istoric indisponibil")
        expect(page.locator("#history-pv")).to_be_hidden()
        failing = False
        page.locator('[data-history="pv"] .history-retry').click()
        expect(page.locator("#history-pv")).to_be_visible()
        assert not errors
        browser.close()


def test_pin_click_drag_manual_coordinates_and_save(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        context = browser.new_context(viewport={"width": 1280, "height": 900}, geolocation={"latitude": 45.123456, "longitude": 25.654321}, permissions=["geolocation"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        page.goto(base + f"/stations/{station_id}/config")
        picker = page.locator("[data-location-picker]")
        picker.scroll_into_view_if_needed()
        expect(picker).to_have_attribute("data-map-ready", "true")
        latitude, longitude = page.locator('[name="latitude"]'), page.locator('[name="longitude"]')
        expect(latitude).to_have_value("44.430000")
        canvas = picker.locator("[data-map-canvas]")
        canvas.click(position={"x": 230, "y": 150})
        assert float(latitude.input_value()) != 44.43
        old_lon = float(longitude.input_value())
        pin = page.locator(".house-map-pin").bounding_box()
        page.mouse.move(pin["x"] + 15, pin["y"] + 15)
        page.mouse.down()
        page.mouse.move(pin["x"] + 80, pin["y"] + 35, steps=8)
        page.mouse.up()
        assert float(longitude.input_value()) != old_lon
        latitude.fill("0")
        longitude.fill("0")
        longitude.press("Tab")
        expect(page.locator(".house-map-pin")).to_be_visible()
        picker.get_by_role("button", name="Foloseste locatia mea").click()
        expect(latitude).to_have_value("45.123456")
        expect(longitude).to_have_value("25.654321")
        page.get_by_role("button", name="Salveaza (creeaza versiune noua)").click()
        expect(latitude).to_have_value("45.123456")
        expect(longitude).to_have_value("25.654321")
        saved = page.request.get(base + f"/stations/{station_id}/data/sun").json()
        assert saved["location"] == {"latitude": 45.123456, "longitude": 25.654321}
        picker.scroll_into_view_if_needed()
        picker.screenshot(path="/tmp/ems-location-picker.png")
        org_link = page.locator('a.breadcrumb-link[href^="/organizations/"]').get_attribute("href")
        page.goto(base + org_link + "/setup/station")
        picker = page.locator("[data-location-picker]")
        picker.scroll_into_view_if_needed()
        expect(picker).to_have_attribute("data-map-ready", "true")
        expect(latitude).to_have_value("")
        expect(longitude).to_have_value("")
        expect(page.locator(".house-map-pin")).to_have_count(0)
        picker.get_by_role("button", name="Fixeaza in centrul hartii").click()
        assert abs(float(latitude.input_value()) - 45.9) < 0.03
        assert abs(float(longitude.input_value()) - 24.9) < 0.05
        pin = page.locator(".house-map-pin").bounding_box()
        canvas = picker.locator("[data-map-canvas]").bounding_box()
        assert abs(pin["x"] + pin["width"] / 2 - canvas["x"] - canvas["width"] / 2) < 2
        assert abs(pin["y"] + pin["height"] / 2 - canvas["y"] - canvas["height"] / 2) < 2
        assert not errors
        browser.close()
