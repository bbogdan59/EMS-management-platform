import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_station_sun_map import _login

pytest_plugins = ["tests.e2e.test_health_diagnostics_ui"]


@pytest.fixture()
def home_assistant_server(monkeypatch, request):
    monkeypatch.setenv("HOME_ASSISTANT_MQTT_ENABLED", "true")
    monkeypatch.setenv(
        "HOME_ASSISTANT_MQTT_BROKERS", '{"Broker de test":"mqtts://broker.example:8883"}'
    )
    return request.getfixturevalue("diagnostics_server")


def test_mapping_consent_download_test_rotation_and_revocation(home_assistant_server):
    base, station_id = home_assistant_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        page.goto(base + f"/stations/{station_id}/integrations/home-assistant")
        form = page.locator("#ha-config-form")
        form.get_by_label("Utilizator MQTT", exact=True).fill("test-mqtt-user")
        form.get_by_label("Parola MQTT", exact=True).fill("test-mqtt-password")
        form.locator('[name="entity_id"]').fill("sensor.boiler_power")
        form.get_by_role("button", name="Adauga senzor").click()
        second = form.locator("[data-ha-mapping]").nth(1)
        second.locator('[name="entity_id"]').fill("binary_sensor.house_occupied")
        second.locator('[name="kind"]').select_option("occupancy")
        expect(second.locator('[name="unit"]')).to_have_value("boolean")
        form.locator('[name="consent"]').check()
        form.locator('[name="publish_consent"]').check()
        form.get_by_role("button", name="Salveaza integrarea").click()
        expect(page.get_by_text("Configuratie salvata.", exact=False)).to_be_visible()
        expect(form.locator('[name="password"]')).to_have_value("")
        expect(page.locator("[data-ha-observations] tr")).to_have_count(2)
        expect(page.locator("[data-ha-observations]")).to_contain_text("Indisponibil")
        with page.expect_download() as download:
            page.get_by_role("link", name="Descarca configuratia Home Assistant").click()
        assert download.value.suggested_filename == "ems-home-assistant.yaml"
        page.get_by_role("button", name="Testeaza conexiunea").click()
        expect(page.locator("[data-ha-status]")).to_have_text("Test in asteptarea workerului")
        page.route(
            "**/integrations/home-assistant/status",
            lambda route: route.fulfill(
                json={
                    "enabled": True,
                    "status": "connected",
                    "error_code": None,
                    "last_connected_at": None,
                    "observations": [
                        {
                            "entity_id": "sensor.boiler_power",
                            "available": False,
                            "value": None,
                            "unit": "kW",
                            "source": "home_assistant",
                            "quality": "stale",
                            "source_quality": "simulated",
                            "observed_at": None,
                        }
                    ],
                }
            ),
        )
        expect(page.locator("[data-ha-observations]")).to_contain_text(
            "expirat · simulat", timeout=10000
        )
        expect(page.locator("[data-ha-observations]")).to_contain_text("Indisponibil")
        page.unroute("**/integrations/home-assistant/status")
        page.reload()
        page.locator(".ha-page").screenshot(path="/tmp/ems-home-assistant-desktop.png")
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
            expect(form.get_by_role("button", name="Salveaza integrarea")).to_be_enabled()
        page.locator(".ha-page").screenshot(path="/tmp/ems-home-assistant-mobile.png")
        page.get_by_text("Credentiale si deconectare", exact=True).click()
        page.get_by_label("Utilizator MQTT nou").fill("rotated-user")
        page.get_by_label("Parola noua", exact=True).fill("rotated-password")
        page.get_by_role("button", name="Salveaza credentialele noi").click()
        expect(page.locator("[data-ha-status]")).to_have_text("Test in asteptarea workerului")
        page.get_by_text("Credentiale si deconectare", exact=True).click()
        page.get_by_role("button", name="Deconecteaza si sterge contextul").click()
        expect(page.locator("[data-ha-status]")).to_have_text("Deconectat · consimtamant retras")
        expect(page.locator("[data-ha-observations]")).to_have_count(0)
        assert not errors
        browser.close()
