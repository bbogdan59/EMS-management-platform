"""Teste HTTP pentru pagina de tarife a statiei (issue #46): persistarea
componentelor noi de cost (distributie/transport/alte taxe/TVA) si preview-ul
de factura-exemplu afisat dupa salvare."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.enums import ImportRunStatus
from app.models.market import ImportRun, MarketPriceInterval
from app.models.tariff import Tariff, TariffVersion
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import get_csrf, login


def _setup(db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Tariff Routes Org")
    admin = make_user(db, email="tariffroutes-admin@test.local", password="Password1234")
    viewer = make_user(db, email="tariffroutes-viewer@test.local", password="Password1234")
    make_membership(db, admin, org, role="organization_admin")
    make_membership(db, viewer, org, role="viewer")
    station = make_station(db, org, admin, name="Tariff Routes Station")
    db.commit()
    return org, admin, viewer, station


def _add_market_interval(db, interval_start: datetime, price_lei_per_kwh: Decimal):
    run = ImportRun(
        source="opcom_pzu", delivery_date=interval_start.date(), revision=1,
        status=ImportRunStatus.succeeded.value, source_url="https://test.local",
        interval_count=1,
    )
    db.add(run)
    db.flush()
    db.add(
        MarketPriceInterval(
            import_run_id=run.id, source="opcom_pzu", delivery_date=interval_start.date(), revision=1,
            interval_index=1, interval_start=interval_start, interval_end=interval_start + timedelta(minutes=15),
            currency="RON", price_lei_per_mwh=price_lei_per_kwh * Decimal("1000"),
            price_lei_per_kwh=price_lei_per_kwh, is_negative=price_lei_per_kwh < 0, is_current=True,
        )
    )
    db.flush()


def test_organization_admin_can_create_fixed_tariff_with_new_components(client, db):
    _org, admin, _viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "fixed", "name": "Fix standard",
            "fixed_price_lei_per_kwh": "0.85", "fixed_monthly_fee_lei": "20", "variable_component_lei_per_kwh": "0",
            "distribution_lei_per_kwh": "0.12", "transport_lei_per_kwh": "0.03", "other_regulated_lei_per_kwh": "0.01",
            "vat_rate_percent": "19", "settlement_method": "net_metering_15min", "settlement_interval_days": "30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    version = db.scalar(select(TariffVersion).join(Tariff).where(Tariff.station_id == station.id))
    assert version is not None
    assert version.distribution_lei_per_kwh == Decimal("0.12")
    assert version.transport_lei_per_kwh == Decimal("0.03")
    assert version.other_regulated_lei_per_kwh == Decimal("0.01")
    assert version.vat_rate_percent == Decimal("19")


def test_viewer_cannot_create_tariff(client, db):
    _org, _admin, viewer, station = _setup(db)
    login(client, viewer.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "fixed", "name": "Blocked",
            "fixed_price_lei_per_kwh": "0.85",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_tariffs_page_shows_invoice_preview_for_fixed_tariff(client, db):
    _org, admin, _viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "fixed", "name": "Fix standard",
            "fixed_price_lei_per_kwh": "0.85", "fixed_monthly_fee_lei": "20", "variable_component_lei_per_kwh": "0",
            "settlement_method": "net_metering_15min", "settlement_interval_days": "30",
        },
        follow_redirects=False,
    )

    resp = client.get(f"/stations/{station.id}/tariffs")
    assert resp.status_code == 200
    assert "Exemplu pentru" in resp.text
    assert "total" in resp.text
    assert "Preview factura-exemplu inainte de salvare" in resp.text
    assert "Import fix: pret din contract + componente" in resp.text
    assert "Custom: formula neimplementata, calcul dezactivat" in resp.text


def test_tariffs_page_shows_reason_when_preview_unavailable_for_indexed_without_price(client, db):
    _org, admin, _viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "indexed_opcom", "name": "Indexat",
            "opcom_margin_lei_per_kwh": "0.10", "fixed_monthly_fee_lei": "0", "variable_component_lei_per_kwh": "0",
            "settlement_method": "net_metering_15min", "settlement_interval_days": "30",
        },
        follow_redirects=False,
    )

    resp = client.get(f"/stations/{station.id}/tariffs")
    assert resp.status_code == 200
    assert "Exemplu indisponibil" in resp.text


def test_tariffs_page_uses_latest_non_future_opcom_price_for_preview(client, db):
    from freezegun import freeze_time

    _org, admin, _viewer, station = _setup(db)
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    _add_market_interval(db, now - timedelta(hours=1), Decimal("0.20"))
    _add_market_interval(db, now + timedelta(days=1), Decimal("0.90"))
    db.commit()

    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "indexed_opcom", "name": "Indexat curent",
            "opcom_margin_lei_per_kwh": "0.10", "fixed_monthly_fee_lei": "0", "variable_component_lei_per_kwh": "0",
            "settlement_method": "net_metering_15min", "settlement_interval_days": "30",
        },
        follow_redirects=False,
    )

    with freeze_time(now):
        resp = client.get(f"/stations/{station.id}/tariffs")

    assert resp.status_code == 200
    assert 'data-market-price="0.200000"' in resp.text
    assert "pret efectiv <strong>0.3000 lei/kWh</strong>" in resp.text
    assert "1.0000 lei/kWh" not in resp.text


def test_fixed_contract_with_stray_opcom_margin_is_rejected_with_error_and_saves_nothing(client, db):
    """Issue #46: `kind` guverneaza efectiv formula -- un contract fix caruia
    i se completeaza si marja OPCOM e respins explicit (nu salvat tacit ca
    fix, ignorand marja, sau invers), iar niciun rand nou nu ajunge in baza
    de date."""
    _org, admin, _viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "fixed", "name": "Fix contaminat",
            "fixed_price_lei_per_kwh": "0.85", "opcom_margin_lei_per_kwh": "0.10",
            "settlement_method": "net_metering_15min", "settlement_interval_days": "30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/stations/" in resp.headers["location"] and "error=" in resp.headers["location"]

    version = db.scalar(select(TariffVersion).join(Tariff).where(Tariff.station_id == station.id))
    assert version is None

    error_page = client.get(resp.headers["location"])
    assert error_page.status_code == 200
    assert "marja fata de OPCOM nu se aplica" in error_page.text
    assert 'data-error-field="opcom_margin_lei_per_kwh"' in error_page.text


def test_dynamic_contract_without_opcom_margin_is_rejected_with_error(client, db):
    """Simetric: contract dinamic-indexat fara marja OPCOM (regula de mapare
    lipsa) e respins explicit, nu salvat cu un pret nedefinit."""
    _org, admin, _viewer, station = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/stations/{station.id}/tariffs",
        data={
            "csrf_token": csrf, "direction": "import", "kind": "indexed_opcom", "name": "Dinamic fara marja",
            "settlement_method": "net_metering_15min", "settlement_interval_days": "30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    version = db.scalar(select(TariffVersion).join(Tariff).where(Tariff.station_id == station.id))
    assert version is None

    error_page = client.get(resp.headers["location"])
    assert "obligatoriu pentru un contract dinamic-indexat" in error_page.text
    assert 'data-error-field="opcom_margin_lei_per_kwh"' in error_page.text
