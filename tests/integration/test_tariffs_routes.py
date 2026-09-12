"""Teste HTTP pentru pagina de tarife a statiei (issue #46): persistarea
componentelor noi de cost (distributie/transport/alte taxe/TVA) si preview-ul
de factura-exemplu afisat dupa salvare."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.core.rate_limit import reset_key
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
