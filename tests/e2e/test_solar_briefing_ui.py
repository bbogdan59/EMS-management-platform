import json
import os
from datetime import UTC, datetime, timedelta

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_health_diagnostics_ui import diagnostics_server  # noqa: F401
from tests.e2e.test_station_sun_map import _login


def snapshot(station_id, count):
    at = datetime.now(UTC).isoformat()
    inputs = []
    for index in range(1, count + 1):
        values = {
            "voltage_v": ("V", "305.2"),
            "current_a": ("A", None if index == 2 else "2.0"),
            "power_w": ("W", "610" if index != 2 else "0"),
        }
        inputs.append(
            {
                "id": str(index),
                "index": index,
                "label": f"MPPT {index}",
                "kind": "mppt",
                "source": "device_rs485",
                "capability": "reported",
                "revision": 0,
                "configuration": {},
                "metrics": {
                    key: {
                        "value": value,
                        "unit": unit,
                        "supported": value is not None,
                        "quality": "missing" if value is None else "measured",
                    }
                    for key, (unit, value) in values.items()
                },
                "flags": [],
                "measured_at": at,
                "received_at": at,
                "freshness": "fresh",
            }
        )
    return {
        "schema_version": 1,
        "station_id": str(station_id),
        "timezone": "Europe/Bucharest",
        "generated_at": at,
        "inverters": [
            {
                "id": "inverter",
                "label": "Acoperisul casei",
                "model": None,
                "source": "device_rs485",
                "inputs": inputs,
                "dc_total_w": "610",
                "dc_quality": "measured",
                "warnings": [],
                "ac_output": {
                    "value": "580",
                    "quality": "measured",
                    "flags": [],
                    "measured_at": at,
                },
            }
        ]
        if count
        else [],
    }


def history():
    start = datetime.now(UTC) - timedelta(days=1)
    return {
        "resolution": "15m",
        "units": {"voltage_v": "V", "current_a": "A", "power_w": "W"},
        "points": [
            {
                "start": (start + timedelta(minutes=15 * i)).isoformat(),
                "power_w": None if i == 30 else str(i * 10),
                "voltage_v": "300",
                "current_a": "2",
                "coverage": {
                    "voltage_v": "1",
                    "current_a": "1",
                    "power_w": "0" if i == 30 else "1",
                },
                "flags": [],
            }
            for i in range(97)
        ],
    }


def test_solar_layout_independent_charts_errors_and_morning_popin(diagnostics_server):  # noqa: F811
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        endpoint = f"**/api/v1/stations/{station_id}/solar"
        for count in (1, 2, 8):
            data = snapshot(station_id, count)
            page.route(
                endpoint,
                lambda route, request, data=data: route.fulfill(
                    content_type="application/json", body=json.dumps(data)
                ),
            )
            page.goto(f"{base}/stations/{station_id}/solar")
            expect(page.locator(".solar-input")).to_have_count(count)
            for width in (320, 390, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                page.screenshot(path=f"/tmp/ems-solar-{count}-{width}.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    page.evaluate(
                        "[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width]).slice(0,20)"
                    )
                )
            page.screenshot(path=f"/tmp/ems-solar-{count}-desktop.png", full_page=True)
            page.unroute(endpoint)

        def serve_history(route):
            requests.append(route.request.url)
            if "/trackers/2/" in route.request.url:
                route.fulfill(status=503, body="{}")
            else:
                route.fulfill(content_type="application/json", body=json.dumps(history()))

        page.route("**/solar/trackers/*/history?*", serve_history)
        assert requests == []
        first, second = page.locator(".solar-input").nth(0), page.locator(".solar-input").nth(1)
        first.get_by_text("Istoric pe intrare", exact=True).click()
        expect(first.locator(".solar-chart")).to_have_attribute("data-chart-ready", "true")
        assert len(requests) == 1 and "/trackers/1/" in requests[0]
        second.get_by_text("Istoric pe intrare", exact=True).click()
        expect(
            second.get_by_text("Istoricul acestei intrari nu a putut fi incarcat.")
        ).to_be_visible()
        expect(first.locator(".solar-chart")).to_have_attribute("data-chart-ready", "true")
        options = first.locator(".solar-chart").evaluate(
            "element => echarts.getInstanceByDom(element).getOption().series[0]"
        )
        assert (
            options["type"] == "line"
            and options["showSymbol"] is False
            and options["connectNulls"] is False
        )
        assert options["data"][30][1] is None
        first.get_by_label("Interval MPPT 1").select_option("7d")
        page.wait_for_function(
            "document.querySelector('.solar-chart').dataset.chartReady === 'true'"
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="Comuta tema").click()
        page.wait_for_function(
            "getComputedStyle(document.querySelector('.solar-panel')).color === 'rgb(240, 244, 232)'"
        )
        assert (
            page.locator(".solar-inverter h3").evaluate("e=>getComputedStyle(e).color")
            == "rgb(240, 244, 232)"
        ), page.evaluate(
            "['.solar-page','.solar-panel','.solar-inverter','.solar-inverter h3','.solar-input','.solar-input h4','.solar-electrical','.solar-input summary'].map(s=>[s,getComputedStyle(document.querySelector(s)).color,getComputedStyle(document.querySelector(s)).getPropertyValue('--solar-ink')])"
        )
        page.screenshot(path="/tmp/ems-solar-dark-viewport.png")
        page.screenshot(path="/tmp/ems-solar-mobile-dark.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.unroute("**/solar/trackers/*/history?*")

        page.route(endpoint, lambda route: route.fulfill(status=503, body="{}"))
        page.reload()
        expect(page.locator("[data-solar-retry]")).to_be_visible()
        page.unroute(endpoint)
        data = snapshot(station_id, 2)
        data["inverters"][0]["inputs"][0]["freshness"] = "stale"
        data["inverters"][0]["inputs"][0]["metrics"]["power_w"]["quality"] = "simulated"
        page.route(
            endpoint,
            lambda route: route.fulfill(content_type="application/json", body=json.dumps(data)),
        )
        page.locator("[data-solar-retry]").click()
        expect(page.locator(".solar-input").first).to_contain_text("neactualizate")
        expect(page.locator(".solar-input").first).to_contain_text("simulat")
        page.unroute(endpoint)
        page.route(
            endpoint,
            lambda route: route.fulfill(
                content_type="application/json", body=json.dumps(snapshot(station_id, 0))
            ),
        )
        page.reload()
        expect(page.locator("[data-solar-status]")).to_contain_text("nu raporteaza inca")

        at = datetime.now(UTC).isoformat()
        notice = {
            "id": "fixture",
            "title": "O zi cu productie peste obiceiul sezonului",
            "severity": "info",
            "created_at": at,
            "read": True,
            "link": f"/stations/{station_id}/briefing/2026-09-29",
            "payload": {
                "kind": "briefing",
                "day": "2026-09-29",
                "body": "Aproximativ 42.3 kWh estimati astazi. Poti muta consumatorii flexibili in jurul orei 12:00.",
            },
        }
        page.route(
            "**/notifications?*",
            lambda route: route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "items": [notice],
                        "unread_count": 0,
                        "has_more": False,
                        "today": "2026-09-29",
                        "timezone": "Europe/Bucharest",
                    }
                ),
            ),
        )
        page.goto(f"{base}/?station_id={station_id}")
        page.locator("[data-notifications-open]").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Tipul notificarilor").select_option("briefing")
        expect(dialog).to_contain_text("42.3 kWh")
        expect(dialog).to_contain_text("Prognoza · estimare")
        expect(dialog.get_by_role("link", name="Vezi planul zilei")).to_have_attribute(
            "href", notice["link"]
        )
        dialog.screenshot(path="/tmp/ems-briefing-mobile.png")
        page.keyboard.press("Escape")
        page.goto(base + "/notifications")
        pref = page.locator("fieldset").filter(has_text="Briefing matinal")
        expect(pref.locator("input[name=morning_briefing]")).not_to_be_checked()
        pref.locator("input[name=morning_briefing]").check()
        pref.locator("input[name=briefing_start_hour]").fill("8")
        page.get_by_role("button", name="Salveaza", exact=True).click()
        expect(page.locator("input[name=morning_briefing]")).to_be_checked()
        expect(page.locator("input[name=briefing_start_hour]")).to_have_value("8")
        assert not errors
        browser.close()
