import os
import re

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_station_sun_map import _login

pytest_plugins = ["tests.e2e.test_health_diagnostics_ui"]


def test_station_notification_popin_filters_read_and_responsive(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        page.goto(base + f"/?station_id={station_id}")
        bell = page.locator("[data-notifications-open]")
        expect(bell).to_have_attribute("aria-label", re.compile("necitite"))
        bell.click()
        dialog = page.get_by_role("dialog", name=re.compile("Notificari"))
        expect(dialog).to_be_visible()
        summary = dialog.locator(".is-summary")
        expect(summary).to_contain_text("Ieri")
        expect(summary).to_contain_text("21,1")
        expect(summary).to_contain_text("Record de productie")
        expect(summary).to_contain_text("O zi fara import din retea")
        expect(summary).to_contain_text("Zi completa")
        dialog.screenshot(path="/tmp/ems-notifications-desktop.png")
        dialog.get_by_label("Tipul notificarilor").select_option("alert")
        expect(summary).to_have_count(0)
        voltage = dialog.locator("article").filter(has=page.get_by_role("heading", name="Tensiune ridicata in retea"))
        expect(voltage).to_contain_text("262.1 V")
        expect(voltage.get_by_role("link", name="Vezi diagnosticul")).to_have_attribute("href", re.compile("/health#alert-"))
        dialog.get_by_label("Tipul notificarilor").select_option("summary")
        expect(summary).to_have_count(1)
        summary.get_by_role("button", name="Marcheaza citita").click()
        expect(summary.get_by_text("Citita", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(bell).to_be_focused()
        page.reload()
        bell.click()
        expect(dialog.locator(".is-summary").get_by_text("Citita", exact=True)).to_be_visible()
        dialog.get_by_label("Tipul notificarilor").select_option("summary")
        dialog.get_by_label("Starea notificarilor").select_option("unread")
        expect(dialog.locator("article")).to_have_count(0)
        expect(dialog.get_by_role("status")).to_contain_text("Nu ai notificari")
        dialog.get_by_label("Tipul notificarilor").select_option("all")
        dialog.get_by_label("Starea notificarilor").select_option("all")
        expect(summary).to_have_count(1)
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
            assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth")
            for _ in range(12):
                page.keyboard.press("Tab")
                assert dialog.evaluate("element => element.contains(document.activeElement)")
            dialog.locator(".notification-scroll").evaluate("element => element.scrollTop = 0")
            dialog.screenshot(path=f"/tmp/ems-notifications-mobile-{width}.png")
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Comuta tema").click()
        bell.click()
        expect(summary).to_have_count(1)
        dialog.screenshot(path="/tmp/ems-notifications-dark.png")
        dialog.get_by_role("button", name="Inchide notificarile").click()
        page.route("**/notifications?*", lambda route: route.fulfill(status=503, body="Unavailable"))
        bell.click()
        expect(dialog.get_by_role("status")).to_contain_text("nu au putut fi incarcate")
        page.unroute("**/notifications?*")
        dialog.get_by_role("button", name="Reincearca").click()
        expect(summary).to_have_count(1)
        assert errors == []
        browser.close()
