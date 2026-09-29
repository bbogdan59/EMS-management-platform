import os
from uuid import uuid4

import pytest
from playwright.sync_api import expect, sync_playwright

from app.core.security import utcnow
from tests.e2e.test_station_sun_map import _login

pytest_plugins = ["tests.e2e.test_health_diagnostics_ui"]


@pytest.fixture()
def bridge_server(monkeypatch, request):
    monkeypatch.setenv("HOME_ASSISTANT_BRIDGE_ENABLED", "true")
    return request.getfixturevalue("diagnostics_server")


def test_shared_temperatures_polling_navigation_and_mobile(bridge_server):
    base, station_id = bridge_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        page.goto(base + f"/stations/{station_id}/integrations/home-assistant-bridge")
        expect(page.locator("[data-ha-empty]")).to_be_visible()
        page.get_by_role("button", name="Genereaza cod de conectare").click()
        expect(page.locator("[data-ha-code]")).not_to_be_empty()
        code = page.locator("[data-ha-code]").inner_text()
        redeemed = page.request.post(
            base + "/api/v1/home-assistant/pairing/redeem",
            data={
                "code": code,
                "instance_id": str(uuid4()),
                "instance_name": "Locuinta de test",
            },
        )
        assert redeemed.ok
        headers = {"Authorization": "Bearer " + redeemed.json()["token"]}
        configured = page.request.put(
            base + "/api/v1/home-assistant/bridge/mappings",
            headers=headers,
            data={
                "expected_version": redeemed.json()["mapping_version"],
                "consent": True,
                "mappings": [
                    {
                        "entity_id": f"sensor.{room}_temperature",
                        "kind": "temperature",
                        "unit": "°C",
                        "device_class": "temperature",
                        "state_class": "measurement",
                        "quality": "estimated",
                    }
                    for room in ("living", "bedroom")
                ],
            },
        )
        assert configured.ok

        def publish(first, second):
            at = utcnow().isoformat()
            response = page.request.post(
                base + "/api/v1/home-assistant/bridge/samples",
                headers=headers,
                data={
                    "mapping_version": configured.json()["mapping_version"],
                    "samples": [
                        {
                            "entity_id": f"sensor.{room}_temperature",
                            "kind": "temperature",
                            "unit": "°C",
                            "source": "home_assistant",
                            "observed_at": at,
                            "sample_id": uuid4().hex,
                            "available": value is not None,
                            "value": value,
                            "quality": "estimated" if value is not None else "unknown",
                        }
                        for room, value in (("living", first), ("bedroom", second))
                    ],
                },
            )
            assert response.ok and response.json()["accepted"] == 2

        publish("21.5", "0")
        page.clock.install()
        page.goto(base + f"/?station_id={station_id}")
        panel = page.locator("[data-ha-bridge]")
        first = panel.locator('[data-ha-entity="sensor.living_temperature"] [data-ha-value]')
        second = panel.locator('[data-ha-entity="sensor.bedroom_temperature"] [data-ha-value]')
        expect(first).to_have_text("21,50 °C")
        expect(second).to_have_text("0,00 °C")
        expect(panel.locator("[data-ha-status]")).to_have_text("Conectat")
        panel.screenshot(path="/tmp/ems-ha-sensors-desktop.png")
        publish("22.75", None)
        page.clock.fast_forward(30000)
        expect(first).to_have_text("22,75 °C")
        expect(second).to_have_text("Indisponibil")
        expect(panel.locator("[data-ha-status]")).to_have_text("Date partiale sau intarziate")
        status_url = base + f"/stations/{station_id}/integrations/home-assistant-bridge/status"
        page.route(status_url, lambda route: route.fulfill(status=503, body="Unavailable"))
        page.clock.fast_forward(30000)
        expect(first).to_have_text("Indisponibil")
        expect(panel.locator("[data-ha-refresh]")).to_contain_text("Nu putem verifica datele")
        page.unroute(status_url)
        publish("23", "19.25")
        page.clock.fast_forward(30000)
        expect(first).to_have_text("23,00 °C")
        expect(second).to_have_text("19,25 °C")
        page.locator("#app-sidebar").get_by_role("link", name="Home Assistant", exact=True).click()
        expect(page).to_have_url(
            base + f"/stations/{station_id}/integrations/home-assistant-bridge"
        )
        expect(first).to_have_text("23,00 °C")
        expect(second).to_have_text("19,25 °C")
        for route in (
            f"/stations/{station_id}/integrations/home-assistant-bridge",
            f"/?station_id={station_id}",
        ):
            page.goto(base + route)
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
                expect(first).to_be_visible()
                expect(second).to_be_visible()
        panel.screenshot(path="/tmp/ems-ha-sensors-mobile.png")
        page.evaluate("document.documentElement.classList.add('dark')")
        panel.screenshot(path="/tmp/ems-ha-sensors-dark.png")
        page.goto(base + f"/stations/{station_id}/integrations/home-assistant-bridge")
        page.get_by_role("button", name="Deconecteaza si sterge contextul").click()
        expect(panel.locator("[data-ha-empty]")).to_be_visible()
        expect(panel.locator("[data-ha-entity]")).to_have_count(0)
        page.goto(base + f"/?station_id={station_id}")
        expect(panel).to_have_count(0)
        assert errors == []
        browser.close()
