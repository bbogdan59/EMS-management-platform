"""Teste pentru design system-ul minim din issue #48: breadcrumb semantic,
grupuri de campuri corelate, controale numerice cu sufix de unitate,
progressive disclosure si evidentiere/focus pe primul camp invalid.

Testele verifica marcajul randat (HTML), nu comportamentul JS in browser --
`test_dashboard_sse_connection_reaches_live_status` (issue #50) e precedentul
de test Playwright real cand un comportament chiar necesita un browser; aici
`data-error-field`/`aria-current` etc. sunt contracte HTML verificabile
direct din raspunsul serverului."""
from __future__ import annotations

import re

from app.core.rate_limit import reset_key
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _setup(db, suffix, role="organization_admin"):
    reset_key("login_attempts:testclient")
    user = make_user(db, email=f"uids-{suffix}@test.local", password="Password1234")
    org = make_org(db, f"UIDS Org {suffix}")
    make_membership(db, user, org, role=role)
    station = make_station(db, org, user, name=f"UIDS Station {suffix}")
    db.commit()
    return user, org, station


def _breadcrumb_hrefs(html: str) -> list[str]:
    return re.findall(r'<a href="([^"]*)" class="breadcrumb-link"', html)


def test_config_page_breadcrumb_shows_organization_and_station_chain(client, db):
    user, org, station = _setup(db, "chain")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200
    assert 'aria-label="breadcrumb"' in resp.text
    assert f">{org.name}<" in resp.text
    assert f">{station.name}<" in resp.text
    assert '<span aria-current="page" class="breadcrumb-current">Configurare</span>' in resp.text


def test_breadcrumb_links_are_relative_no_open_redirect(client, db):
    """Toate href-urile breadcrumb-ului trebuie sa fie cai relative construite
    din ID-uri deja autorizate (niciodata absolute/externe) -- fara risc de
    open-redirect prin breadcrumb."""
    user, org, station = _setup(db, "relurl")
    login(client, user.email, "Password1234")

    for path in (
        f"/organizations/{org.id}",
        f"/stations/{station.id}/config",
        f"/stations/{station.id}/preferences",
        f"/stations/{station.id}/devices",
        f"/stations/{station.id}/tariffs",
    ):
        resp = client.get(path)
        assert resp.status_code == 200
        hrefs = _breadcrumb_hrefs(resp.text)
        assert hrefs, f"niciun link breadcrumb gasit pe {path}"
        for href in hrefs:
            assert href.startswith("/"), f"link breadcrumb absolut/extern pe {path}: {href}"
            assert not href.startswith("//"), f"link breadcrumb schema-relativ (open-redirect) pe {path}: {href}"


def test_breadcrumb_present_for_viewer_role_readonly_page(client, db):
    """Breadcrumb-ul e independent de `can_edit` -- un viewer fara drept de
    editare tot vede navigatia contextuala pe pagina read-only."""
    user, org, station = _setup(db, "viewer", role="viewer")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200
    assert 'aria-label="breadcrumb"' in resp.text
    assert "id=\"config-form\"" not in resp.text  # viewer nu vede formularul de editare


def test_config_page_field_groups_and_unit_suffixes(client, db):
    user, org, station = _setup(db, "groups")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200
    assert '<legend class="field-group-legend">Sistem PV / invertor</legend>' in resp.text
    assert '<legend class="field-group-legend">Capacitate baterie</legend>' in resp.text
    assert '<legend class="field-group-legend">Putere baterie</legend>' in resp.text
    # Sufixele de unitate sunt decorative (aria-hidden), nu modifica name-ul campului.
    assert '<span class="input-unit-suffix" aria-hidden="true">kW</span>' in resp.text
    assert '<span class="input-unit-suffix" aria-hidden="true">kWh</span>' in resp.text
    assert 'name="pv_installed_power_kw"' in resp.text


def test_config_page_has_progressive_disclosure_for_grid_limits(client, db):
    user, org, station = _setup(db, "disclosure")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config")
    assert resp.status_code == 200
    assert "Setari avansate: limite retea" in resp.text
    # Campurile avansate exista in pagina (in <details>), doar ascunse implicit de browser.
    assert 'name="grid_import_limit_kw"' in resp.text
    assert 'name="grid_export_limit_kw"' in resp.text


def test_config_error_summary_maps_field_name_for_inline_highlight(client, db):
    """`data-error-field` trebuie sa corespunda exact numelui `name` al
    inputului din pagina, ca `form-errors.js` sa poata evidentia/focaliza
    campul corect (contract HTML verificat direct, fara browser)."""
    user, org, station = _setup(db, "errfield")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/config?error=pv_installed_power_kw%3A+Input+should+be+greater+than+0")
    assert resp.status_code == 200
    assert 'data-error-summary' in resp.text
    assert '<li data-error-field="pv_installed_power_kw">pv_installed_power_kw: Input should be greater than 0</li>' in resp.text
    assert 'name="pv_installed_power_kw"' in resp.text


def test_organization_detail_breadcrumb_and_lat_lng_group(client, db):
    user, org, station = _setup(db, "orgdetail")
    login(client, user.email, "Password1234")

    resp = client.get(f"/organizations/{org.id}")
    assert resp.status_code == 200
    assert 'aria-label="breadcrumb"' in resp.text
    assert f'<span aria-current="page" class="breadcrumb-current">{org.name}</span>' in resp.text
    assert '<legend class="field-group-legend">Locatie (necesara pentru prognoza PV)</legend>' in resp.text
    assert 'name="latitude"' in resp.text
    assert 'name="longitude"' in resp.text


def test_preferences_page_soc_field_group(client, db):
    user, org, station = _setup(db, "prefsoc")
    login(client, user.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/preferences")
    assert resp.status_code == 200
    assert 'aria-label="breadcrumb"' in resp.text
    assert '<legend class="field-group-legend">SOC baterie</legend>' in resp.text
    assert 'name="min_reserve_soc_percent"' in resp.text
    assert 'name="max_normal_soc_percent"' in resp.text


def test_admin_organization_detail_breadcrumb(client, db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="uids-admin@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "UIDS Admin Org")
    db.commit()
    login(client, admin.email, "Password1234")

    resp = client.get(f"/admin/organizations/{org.id}")
    assert resp.status_code == 200
    assert 'aria-label="breadcrumb"' in resp.text
    assert ">Administrare<" in resp.text
    assert ">Organizatii<" in resp.text
    hrefs = _breadcrumb_hrefs(resp.text)
    assert all(href.startswith("/") for href in hrefs)
