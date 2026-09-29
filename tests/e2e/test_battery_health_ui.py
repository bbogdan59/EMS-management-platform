"""Real authenticated page plus deterministic visual scenarios; no hardware claims."""
import copy
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_health_diagnostics_ui import diagnostics_server  # noqa: F401


def visual_data(original, scenario):
    data = copy.deepcopy(original)
    live = data["current"]
    for field, value in {"soc": "65", "power": "-0.6", "temperature": "24", "voltage": "51.2", "current": "-11.7",
                         "nominal_capacity": "10.2", "usable_capacity": "9.8", "stored_energy": "6.63", "reported_cycles": "50"}.items():
        live[field].update(value=value, quality="measured", supported=True)
    live["soh"].update(value="96", status="measured", quality="measured", method="source_reported", confidence="unknown")
    live["state"] = "discharging"
    live["freshness"] = "fresh"
    data["notices"] = []
    for period in (data["today"], data["period"]):
        period.update(charge_kwh="9.9", discharge_kwh="11.3", coverage="0.85", quality="measured", efc="1.04", efc_quality="estimated", efc_reason="partial_history")
    for buckets in (data["hourly"], data["daily"], data["temperature_days"]):
        for index, bucket in enumerate(buckets):
            bucket.update(charge_kwh=str((index % 5) / 3), discharge_kwh=str((index % 4) / 4), soc_percent=str(20 + index % 8 * 10), temperature_min_c="18", temperature_max_c="32")
            bucket["coverage"] = dict.fromkeys(bucket["coverage"], "0.85")
            bucket["quality"] = dict.fromkeys(bucket["quality"], "measured")
    data["extremes"] = [{"kind": "coldest", "value_c": "18", "measured_at": data["generated_at"], "quality": "measured"},
                        {"kind": "warmest", "value_c": "32", "measured_at": data["generated_at"], "quality": "measured"}]
    if scenario in ("hot", "cold"):
        live["temperature"]["value"] = "55" if scenario == "hot" else "-2"
        data["notices"] = [{"code": scenario, "severity": "warning", "title": "Temperatura ridicata" if scenario == "hot" else "Temperatura sub 0 °C",
                            "explanation": "Consulta limitele producatorului si verifica instalatia."}]
    elif scenario == "stale":
        live["freshness"] = "stale"
        for metric in ("soc", "power", "temperature", "soh"):
            live[metric]["quality"] = "stale"
        data["notices"] = [{"code": "stale", "severity": "warning", "title": "Date neactualizate", "explanation": "Ultimele observatii disponibile."}]
    elif scenario == "unsupported":
        live["target"]["source"] = "deye_cloud"
        for metric in ("temperature", "voltage", "current", "soh", "reported_cycles"):
            live[metric].update(value=None, quality="missing", supported=False)
        live["soh"]["status"] = "unavailable"
        for bucket in data["temperature_days"]:
            bucket.update(temperature_min_c=None, temperature_max_c=None)
            bucket["coverage"]["temperature"] = "0"
            bucket["quality"]["temperature"] = "missing"
        data["extremes"] = []
    elif scenario == "empty":
        data["current"] = None
        data["targets"] = []
    return data


def test_battery_dashboard_mobile_states_and_interactions(diagnostics_server):  # noqa: F811
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1, has_touch=True, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/login")
        page.fill("#email", "diagnostics-browser@test.local")
        page.fill("#password", "TestPass1234")
        page.click("button[type=submit]")
        path = f"/api/v1/stations/{station_id}/battery-health"
        original = page.request.get(base + path).json()
        page.goto(f"{base}/stations/{station_id}/battery")
        expect(page.locator("#battery-content")).to_be_visible()
        expect(page.locator("#battery-notices")).to_contain_text("Temperatura ridicata")
        for scenario in ("healthy", "hot", "cold", "stale", "unsupported", "empty"):
            payload = visual_data(original, scenario)
            page.route("**/battery-health?*", lambda route, request, payload=payload: route.fulfill(content_type="application/json", body=json.dumps(payload)))
            page.reload()
            expect(page.locator("#battery-content")).to_be_visible()
            if scenario == "healthy":
                expect(page.locator("#battery-soc")).to_have_text("65%")
                expect(page.locator("#battery-state")).to_contain_text("Descarcare")
                expect(page.locator("#battery-efc-meta")).to_contain_text("Istoric partial")
                expect(page.locator("#battery-hourly-chart")).to_have_attribute("data-chart-ready", "true")
                page.locator(".battery-table summary").first.click()
                expect(page.locator("#battery-hourly-table table")).to_be_visible()
                page.locator(".battery-table summary").first.click()
            elif scenario in ("hot", "cold", "stale"):
                expect(page.locator("#battery-notices")).to_contain_text(payload["notices"][0]["title"])
            elif scenario == "unsupported":
                expect(page.locator("#battery-soh")).to_have_text("Indisponibil")
                expect(page.locator("#battery-temp-range")).to_have_text("Fara date de temperatura")
            else:
                expect(page.locator("#battery-state")).to_have_text("Fara sursa conectata")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.evaluate("document.documentElement.classList.add('dark'); document.dispatchEvent(new CustomEvent('ems:theme-change'))")
            page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
            page.screenshot(path=f"/tmp/ems-battery-{scenario}.png", full_page=True, animations="disabled")
            page.unroute("**/battery-health?*")
        page.route("**/battery-health?*", lambda route: route.fulfill(status=503, body="{}"))
        page.reload()
        expect(page.locator("#battery-error")).to_be_visible()
        expect(page.locator("#battery-content")).to_be_hidden()
        page.unroute("**/battery-health?*")
        page.locator("#battery-retry").click()
        expect(page.locator("#battery-content")).to_be_visible()
        page.locator("#battery-days").select_option("7")
        expect(page.locator("#battery-period-title")).to_have_text("7 zile · energie")
        selected = datetime.fromisoformat(original["daily"][-2]["start"]).astimezone(ZoneInfo(original["timezone"])).date().isoformat()
        page.locator("#battery-day").fill(selected)
        page.locator("#battery-day").dispatch_event("change")
        expect(page.locator("#battery-day-title")).to_have_text("Ziua selectata")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.screenshot(path="/tmp/ems-battery-desktop.png", full_page=True, animations="disabled")
        assert not errors
        browser.close()
