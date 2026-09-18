"""Teste HTTP pentru "Dashboard client one station first" (issue #45):

- 0 statii: clientul primeste un empty state explicativ (nu un fals 200 gol),
  fara nicio incercare de redirect catre o statie inexistenta.
- 1 statie: clientul ajunge DIRECT la dashboard-ul acelei statii, fara sa mai
  treaca printr-un selector cu o singura optiune -- selectorul din navbar nici
  nu se mai afiseaza.
- mai multe statii: comportamentul actual (selector + pagina de alegere)
  ramane neschimbat -- selectorul apare doar cand chiar e necesar.
- administratorii de platforma (care vad TOATE statiile din sistem, nu doar
  ale lor) sunt exclusi explicit din auto-redirect, ca sa nu fie teleportati
  implicit intr-o statie oarecare doar pentru ca sistemul are, la un moment
  dat, o singura statie inregistrata.
- RBAC: rolul de membership (viewer/operator/organization_admin) nu schimba
  acest comportament de navigare -- oricare dintre ele ajunge direct la
  dashboard cand are o singura statie.

Acopera si afisarea explicatiilor "Cum se calculeaza?" adaugate pe KPI-urile
de cost efectiv import/export si pe cele doua KPI-uri de beneficiu."""
from __future__ import annotations

from app.core.rate_limit import reset_key
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def test_zero_stations_shows_explained_empty_state(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="ds-zero@test.local", password="Password1234")
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 200
    assert "Nu ai acces la nicio statie" in resp.text
    # Nu exista niciun selector de statie (nimic de ales) si nici o redirectare.
    assert 'name="station_id"' not in resp.text


def test_single_station_user_redirects_straight_to_dashboard(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="ds-one@test.local", password="Password1234")
    org = make_org(db, "DS One Station Org")
    make_membership(db, user, org, role="organization_admin")
    station = make_station(db, org, user, name="DS Only Station")
    db.commit()

    login(client, user.email, "Password1234")

    redirect_resp = client.get("/", follow_redirects=False)
    assert redirect_resp.status_code == 302
    assert redirect_resp.headers["location"] == f"/?station_id={station.id}"

    page = client.get("/", follow_redirects=True)
    assert page.status_code == 200
    assert "DS Only Station" in page.text
    # Cu o singura statie disponibila, selectorul multi-statie nu se mai afiseaza
    # deloc (doar numele statiei, ca text simplu) -- issue #45.
    assert 'name="station_id"' not in page.text
    assert "DS One Station Org / DS Only Station" in page.text
    assert f'href="/?station_id={station.id}" class="flex items-center gap-2 text-base font-semibold' in page.text


def test_single_station_redirect_does_not_reflect_untrusted_host(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="ds-host@test.local", password="Password1234")
    org = make_org(db, "DS Host Org")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="DS Host Station")
    db.commit()

    login(client, user.email, "Password1234")
    resp = client.get("/", headers={"host": "attacker.invalid"}, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == f"/?station_id={station.id}"


def test_multi_station_user_sees_picker_with_selector(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="ds-multi@test.local", password="Password1234")
    org = make_org(db, "DS Multi Station Org")
    make_membership(db, user, org, role="organization_admin")
    make_station(db, org, user, name="DS Station A")
    make_station(db, org, user, name="DS Station B")
    db.commit()

    login(client, user.email, "Password1234")

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200  # nicio redirectare automata cand sunt mai multe statii
    assert 'name="station_id"' in resp.text
    assert "DS Station A" in resp.text
    assert "DS Station B" in resp.text


def test_platform_admin_not_auto_redirected_with_single_system_station(client, db):
    """Un admin de platforma vede TOATE statiile din sistem, nu pe ale lui --
    daca sistemul are (la acel moment) o singura statie, asta nu inseamna ca
    admin-ul e "clientul cu o singura statie" din issue #45, deci nu trebuie
    teleportat automat in ea."""
    reset_key("login_attempts:testclient")
    owner = make_user(db, email="ds-owner@test.local", password="Password1234")
    org = make_org(db, "DS Admin Visible Org")
    make_membership(db, owner, org, role="viewer")
    make_station(db, org, owner, name="DS Admin Visible Station")

    admin = make_user(db, email="ds-admin@test.local", password="Password1234", is_platform_admin=True)
    db.commit()

    login(client, admin.email, "Password1234")
    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 200  # nu 302 -- fara auto-redirect pentru admin
    assert 'name="station_id"' in resp.text


def test_viewer_role_reaches_single_station_dashboard(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="ds-viewer@test.local", password="Password1234")
    org = make_org(db, "DS Viewer Org")
    make_membership(db, user, org, role="viewer")
    make_station(db, org, user, name="DS Viewer Station")
    db.commit()

    login(client, user.email, "Password1234")
    page = client.get("/", follow_redirects=True)

    assert page.status_code == 200
    assert "DS Viewer Station" in page.text


def test_no_membership_user_not_redirected_into_unrelated_station(client, db):
    """Un utilizator fara niciun membership nu trebuie sa vada/atinga statii
    ale altor organizatii, indiferent cate exista in sistem (RBAC)."""
    reset_key("login_attempts:testclient")
    other_user = make_user(db, email="ds-other@test.local", password="Password1234")
    other_org = make_org(db, "DS Other Org")
    make_membership(db, other_user, other_org, role="organization_admin")
    make_station(db, other_org, other_user, name="DS Other Station")

    outsider = make_user(db, email="ds-outsider@test.local", password="Password1234")
    db.commit()

    login(client, outsider.email, "Password1234")
    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 200
    assert "DS Other Station" not in resp.text
    assert "Nu ai acces la nicio statie" in resp.text


def test_price_and_benefit_kpi_cards_have_calculation_disclosure(client, db):
    reset_key("login_attempts:testclient")
    user = make_user(db, email="ds-formula@test.local", password="Password1234")
    org = make_org(db, "DS Formula Org")
    make_membership(db, user, org, role="organization_admin")
    station = make_station(db, org, user, name="DS Formula Station")
    db.commit()

    login(client, user.email, "Password1234")
    page = client.get(f"/?station_id={station.id}")

    assert page.status_code == 200
    # Costurile/beneficiile si KPI-urile Astazi/Luna curenta au disclosure-uri
    # explicite; testul cere existenta lor, nu un numar inghetat de carduri.
    assert page.text.count("Cum se calculeaza?") >= 6
    assert "Astazi" in page.text
    assert "Luna curenta" in page.text
    assert "Pe scurt, ce se intampla acum" in page.text
    assert "Flux energetic actual" in page.text
    assert "Produce acum" in page.text
    assert "Consuma acum" in page.text
    assert "Analiza avansata: preturi, planuri si prognoze" in page.text
    assert "nu este inlocuita cu zero" in page.text
    assert f"/stations/{station.id}/tariffs" in page.text
