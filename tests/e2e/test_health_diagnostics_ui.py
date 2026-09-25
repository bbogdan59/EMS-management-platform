from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from playwright.sync_api import expect, sync_playwright
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.telemetry import TelemetryRaw
from app.services import health_service, notification_service
from tests.factories import make_device, make_membership, make_org, make_station, make_user


@pytest.fixture()
def diagnostics_server():
    name = "ems_diagnostics_e2e_" + uuid4().hex[:10]
    with psycopg.connect("postgresql://ems:ems@localhost:5432/ems", autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    root = Path(__file__).parents[2]
    url = f"postgresql+psycopg://ems:ems@localhost:5432/{name}"
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "ENVIRONMENT": "test",
        "SECRET_KEY": "diagnostics-e2e-secret",
        "SESSION_COOKIE_SECURE": "false",
        "ENERGY_ASSISTANT_ENABLED": "true",
        "NOTIFICATIONS_EMAIL_ENABLED": "false",
        "NOTIFICATIONS_PUSH_ENABLED": "false",
        "REDIS_URL": "redis://localhost:6379/2",
    }
    process = None
    engine = create_engine(url)
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
        )
        with Session(engine, expire_on_commit=False) as db:
            user = make_user(db, email="diagnostics-browser@test.local")
            org = make_org(db, "Browser Diagnostics")
            make_membership(db, user, org, "organization_admin")
            station = make_station(db, org, user, name="Browser Station")
            device = make_device(db, station)
            at = utcnow().replace(second=0, microsecond=0)
            at = at.replace(minute=at.minute // 5 * 5)
            device.last_heartbeat_at = at
            db.add(
                TelemetryRaw(
                    station_id=station.id,
                    device_id=device.id,
                    boot_id="browser",
                    sequence=1,
                    measured_at=at,
                    received_at=at,
                    pv_power_w=Decimal(1000),
                    battery_soc_percent=Decimal(50),
                    diagnostics={"battery": {"quality": "measured", "temperature_c": "80"}},
                )
            )
            db.flush()
            health_service.evaluate_station(db, station, at)
            notification_service.materialize(db, at)
            db.commit()
            station_id = station.id
        with socket.socket() as server_socket:
            server_socket.bind(("127.0.0.1", 0))
            port = server_socket.getsockname()[1]
        with open("/tmp/ems-diagnostics-browser-server.log", "w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        base = f"http://127.0.0.1:{port}"
        for _ in range(80):
            try:
                if httpx.get(base + "/health").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Diagnostic browser server did not start")
        yield base, station_id
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
        engine.dispose()
        with psycopg.connect(
            "postgresql://ems:ems@localhost:5432/ems", autocommit=True
        ) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_health_notification_and_assistant_browser_flow(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/login")
        page.fill("#email", "diagnostics-browser@test.local")
        page.fill("#password", "TestPass1234")
        page.click("button[type=submit]")
        page.goto(base + "/fleet/health")
        page.get_by_role("link", name="Browser Station", exact=True).click()
        expect(page.locator("h1")).to_contain_text("Browser Station")
        incident = page.locator("article").filter(
            has=page.get_by_role("heading", name="Temperatura bateriei ridicata")
        )
        incident.locator("input[name=reason]").fill("Verificare de test")
        incident.get_by_role("button", name="Am vazut").click()
        expect(incident).to_contain_text("acknowledged")
        page.locator("#energy-assistant-form input[name=question]").fill("Cat am importat ieri?")
        page.locator("#energy-assistant-form button").click()
        expect(page.locator("#energy-assistant-answer")).to_contain_text("Concluzie indisponibila")
        with page.expect_download() as download_info:
            page.get_by_role("link", name="Export diagnostic (ultimele 24 ore)").click()
        assert download_info.value.suggested_filename.endswith(".json")
        page.goto(base + "/notifications")
        expect(page.locator("h1")).to_have_text("Notificari")
        prefs = page.locator('form[action$="notification-preferences"]')
        prefs.locator("details summary").click()
        prefs.locator('select[name="matrix:warning:warning:email"]').select_option("daily")
        prefs.get_by_role("button", name="Salveaza").click()
        expect(page.locator('select[name="matrix:warning:warning:email"]')).to_have_value("daily")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(base + f"/stations/{station_id}/health")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path="/tmp/ems-diagnostics-mobile.png", full_page=True)
        assert errors == []
        browser.close()


def test_energy_operations_ev_schedule_preset_and_control_browser_flow(diagnostics_server):
    base, station_id = diagnostics_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/login")
        page.fill("#email", "diagnostics-browser@test.local")
        page.fill("#password", "TestPass1234")
        page.click("button[type=submit]")
        page.goto(base + f"/stations/{station_id}/ev")
        page.get_by_text("Adauga o statie EV cu un conector", exact=True).click()
        form = page.locator(f'form[action="/stations/{station_id}/ev"]')
        form.locator('[name="name"]').fill("Garage EV")
        form.locator('[name="max_power_kw"]').fill("7.4")
        form.locator('[name="meter"]').check()
        form.get_by_role("button", name="Adauga EVSE").click()
        expect(page.locator("h2").filter(has_text="Garage EV")).to_be_visible()
        page.get_by_text("Cerinta de plecare / program saptamanal", exact=True).click()
        form = page.locator('form[action$="/ev/requirements"]')
        form.locator('[name="energy"]').fill("12")
        form.locator('[name="weekdays"][value="0"]').check()
        form.locator('[name="local_time"]').fill("08:00")
        form.get_by_role("button", name="Salveaza cerinta").click()
        expect(page.locator("article").first).to_contain_text("12,00 kWh")
        page.goto(base + f"/stations/{station_id}/recommendations")
        page.locator('form[action$="/recommendations/preset"] select').select_option("economy")
        page.locator('form[action$="/recommendations/preset"] button').click()
        expect(page.locator("h1")).to_have_text("Economy")
        expect(page.get_by_role("button", name="Apply — confirm modificarile")).to_be_disabled()
        page.locator('[name="reason"]').fill("Browser feedback")
        page.get_by_role("button", name="Not relevant", exact=True).click()
        expect(page.locator("main")).to_contain_text("not_relevant")
        page.set_viewport_size({"width": 390, "height": 844})
        for route in ("ev", "control", "recommendations"):
            page.goto(base + f"/stations/{station_id}/{route}")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), route
        page.screenshot(path="/tmp/ems-operations-mobile.png", full_page=True)
        assert errors == []
        browser.close()
