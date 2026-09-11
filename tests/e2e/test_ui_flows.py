"""Teste Playwright pentru fluxurile UI principale (sectiunea 14 din cerinte):
bootstrap admin -> login -> creare organizatie -> creare statie -> dashboard.
Necesita un server live (fixture `live_server`) si un browser Chromium
(pre-instalat in mediul de dezvoltare)."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect


@pytest.fixture(scope="session")
def browser():
    import glob

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # Mediul poate avea o versiune de Chromium pre-instalata diferita de cea
        # asteptata de pachetul `playwright` pinuit -- folosim binarul disponibil explicit.
        candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
        executable_path = candidates[0] if candidates else None
        b = p.chromium.launch(executable_path=executable_path)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    pg = context.new_page()
    yield pg
    context.close()


def test_bootstrap_login_create_org_and_station_flow(live_server, page):
    base = live_server

    page.goto(f"{base}/bootstrap-admin")
    page.fill("#bootstrap_token", "e2e-bootstrap-token")
    page.fill("#full_name", "E2E Admin")
    page.fill("#email", "e2e-admin@test.local")
    page.fill("#password", "E2ETestPassword123")
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{base}/login")

    page.fill("#email", "e2e-admin@test.local")
    page.fill("#password", "E2ETestPassword123")
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{base}/")

    page.goto(f"{base}/admin/organizations")
    page.fill("input[name=name]", "E2E Test Org")
    page.click("button:has-text('Creeaza')")
    expect(page.locator("text=E2E Test Org")).to_be_visible()

    page.click("text=deschide")
    expect(page.locator("h1")).to_have_text("E2E Test Org")

    page.click("summary:has-text('Adauga statie')")
    page.fill("input[name=name]", "E2E Test Station")
    page.fill("input[name=latitude]", "44.43")
    page.fill("input[name=longitude]", "26.10")
    page.fill("input[name=pv_installed_power_kw]", "5")
    page.fill("input[name=inverter_power_kw]", "5")
    page.click("button:has-text('Creeaza statia')")

    station_link = page.get_by_role("link", name="E2E Test Station")
    expect(station_link).to_be_visible()

    station_link.click()
    expect(page.locator("h1")).to_contain_text("E2E Test Station")


def test_login_rejects_wrong_password(live_server, page):
    base = live_server
    page.goto(f"{base}/login")
    page.fill("#email", "e2e-admin@test.local")
    page.fill("#password", "wrong-password")
    page.click("button[type=submit]")
    expect(page.locator("text=Email sau parola incorecte")).to_be_visible()


def test_dark_mode_toggle_persists(live_server, page):
    base = live_server
    page.goto(f"{base}/login")
    page.fill("#email", "e2e-admin@test.local")
    page.fill("#password", "E2ETestPassword123")
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{base}/")

    html_classes_before = page.eval_on_selector("html", "el => el.className")
    page.click("button[title='Comuta tema']")
    html_classes_after = page.eval_on_selector("html", "el => el.className")
    assert html_classes_before != html_classes_after
