import os

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_station_sun_map import _login

pytest_plugins = ["tests.e2e.test_health_diagnostics_ui"]


def test_invoice_setup_preview_save_and_mobile(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 1440, "height": 1040}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        page.goto(base + f"/stations/{station_id}/tariffs")
        form = page.locator("#ro-tariff-form")
        expect(form.locator("[data-ro-prices]")).to_be_hidden()
        form.get_by_role("button", name="Completeaza exemplul").click()
        expect(form.locator("[data-ro-import-price]")).to_have_text("1,15732")
        expect(form.locator("[data-ro-export-price]")).to_have_text("0,44637")
        expect(form.locator("[data-ro-total]")).to_have_text("257,92 lei")
        page.locator(".ro-tariffs").screenshot(path="/tmp/ems-tariff-setup-desktop.png")
        form.locator("[data-ro-import-kwh]").fill("100")
        form.locator("[data-ro-export-kwh]").fill("100")
        expect(form.locator("[data-ro-total]")).to_have_text("71,10 lei")
        expect(form.locator("[data-ro-compensation]")).to_contain_text("100,00 kWh compensati")
        form.locator('[name="tg_in_active"]').check()
        expect(form.locator("[data-ro-import-price]")).to_have_text("1,15293")
        expect(form.locator("[data-ro-export-price]")).to_have_text("0,44637")
        form.locator('[name="tg_in_active"]').uncheck()
        form.locator("[data-ro-export-kwh]").fill("300")
        expect(form.locator("[data-ro-surplus]")).to_contain_text("200,00 kWh surplus")
        expect(form.locator("[data-ro-total]")).to_have_text("71,10 lei")
        form.locator("[data-ro-import-kwh]").fill("")
        expect(form.locator("[data-ro-month]")).to_be_hidden()
        expect(form.locator("[data-ro-status]")).to_contain_text("valoare lipsa")
        form.locator("[data-ro-import-kwh]").fill("0")
        expect(form.locator("[data-ro-total]")).to_have_text("0,00 lei")
        form.locator('[name="cfd"]').fill("0,000144")
        expect(form.locator("[data-ro-prices]")).to_be_visible()
        form.locator('[name="name"]').fill("Contract din factura")
        form.locator(".ro-validity > summary").click()
        form.locator('[name="effective_from"]').fill("2026-08-01T00:00")
        form.get_by_role("button", name="Salveaza tarifele de prosumator").click()
        expect(page.locator(".ro-saved")).to_be_visible()
        expect(form.locator('[name="green_certificates"]')).to_have_value("0.07401920")
        expect(form.locator('[name="cfd"]')).to_have_value("0.000144")
        expect(form.locator("[data-ro-import-price]")).to_have_text("1,15732")
        page.get_by_text("Tarife salvate si istoric", exact=True).click()
        expect(page.locator(".tariff-history")).to_contain_text("Compensare lunara 1:1")
        expect(page.locator(".tariff-history .ro-breakdown-history")).to_have_count(2)
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
            expect(form.get_by_role("button", name="Salveaza tarifele de prosumator")).to_be_enabled()
            page.locator(".ro-preview").screenshot(path=f"/tmp/ems-tariff-preview-{width}.png")
        page.get_by_role("button", name="Comuta tema").click()
        page.locator(".ro-preview").screenshot(path="/tmp/ems-tariff-preview-dark.png")
        page.route("**/tariffs/romania/preview", lambda route: route.fulfill(status=503, body="Unavailable"))
        form.locator('[name="active_energy"]').fill("0.46")
        expect(form.locator("[data-ro-status]")).to_contain_text("nu a putut fi incarcat")
        expect(form.locator("[data-ro-prices]")).to_be_hidden()
        page.unroute("**/tariffs/romania/preview")
        form.locator('[name="active_energy"]').fill("0.45")
        expect(form.locator("[data-ro-import-price]")).to_have_text("1,15732")
        assert errors == []
        browser.close()
