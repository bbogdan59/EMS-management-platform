"""Teste HTTP pentru endpoint-ul de economii/beneficiu al dashboard-ului
(issue #49): detalierea financiara (valoare bruta PV, economie autoconsum,
venit export) si indicatorul de provenienta a tarifului (masurat/estimat)
trebuie sa ajunga in raspunsul JSON real al rutei, nu doar in serviciu."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.rate_limit import reset_key
from app.core.security import utcnow
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryAggregate
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def _setup(db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Savings Route Org")
    viewer = make_user(db, email="savingsroute-viewer@test.local", password="Password1234")
    make_membership(db, viewer, org, role="viewer")
    station = make_station(db, org, viewer, name="Savings Route Station")

    t0 = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)
    import_tariff = Tariff(station_id=station.id, direction="import", kind="fixed", name="Fix import")
    export_tariff = Tariff(station_id=station.id, direction="export", kind="fixed", name="Fix export")
    db.add_all([import_tariff, export_tariff])
    db.flush()
    db.add(TariffVersion(
        tariff_id=import_tariff.id, valid_from=t0 - timedelta(days=1), fixed_price_lei_per_kwh=Decimal("1.0"),
        opcom_margin_lei_per_kwh=None, fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
    ))
    db.add(TariffVersion(
        tariff_id=export_tariff.id, valid_from=t0 - timedelta(days=1), fixed_price_lei_per_kwh=Decimal("0.4"),
        opcom_margin_lei_per_kwh=None, fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
    ))
    db.add(TelemetryAggregate(
        station_id=station.id, period_type="hour", period_start=t0, period_end=t0 + timedelta(hours=1),
        load_energy_kwh=Decimal("5"), pv_energy_kwh=Decimal("8"),
        grid_import_energy_kwh=Decimal("0"), grid_export_energy_kwh=Decimal("3"),
        coverage={"load": 1.0, "pv": 1.0, "grid": 1.0},
    ))
    db.commit()
    return station, viewer, t0


def test_data_savings_route_exposes_financial_breakdown_and_provenance(client, db):
    station, viewer, t0 = _setup(db)
    login(client, viewer.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/data/savings?range=30d")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    # 8 kWh PV * 1.0 lei/kWh pret de cumparare.
    assert abs(body["gross_pv_value_lei"] - 8.0) < 0.01
    # 5 kWh autoconsumate * 1.0 lei/kWh.
    assert abs(body["self_consumption_savings_lei"] - 5.0) < 0.01
    # 3 kWh exportate * 0.4 lei/kWh.
    assert abs(body["export_revenue_lei"] - 1.2) < 0.01
    assert body["tariff_buy_provenance"]["fixed_contract"] == 1
    assert body["tariff_provenance_summary"] == "measured"
    # Descrierile trebuie sa fie prezente si nevide -- UI-ul le afiseaza ca tooltip/formula.
    assert body["gross_pv_value_description"]
    assert body["self_consumption_savings_description"]
    assert body["export_revenue_description"]


def test_data_savings_route_requires_station_access(client, db):
    _org2 = make_org(db, "Other Org")
    outsider = make_user(db, email="savingsroute-outsider@test.local", password="Password1234")
    station, _viewer, _t0 = _setup(db)
    login(client, outsider.email, "Password1234")

    resp = client.get(f"/stations/{station.id}/data/savings?range=30d")

    assert resp.status_code == 403
