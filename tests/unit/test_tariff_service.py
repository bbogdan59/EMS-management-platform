"""Teste numerice pentru formula de cost efectiv (issue #46): separarea
cost marginal / cost fix, componente de retea/taxe distincte, TVA explicit
opt-in, blocarea calculului cand lipseste pretul OPCOM (fara fallback
tacut), pret negativ, si preview-ul de factura-exemplu."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.tariff import TariffVersion
from app.services import tariff_service as svc


def _version(**overrides) -> TariffVersion:
    defaults = {
        "tariff_id": uuid.uuid4(),
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "fixed_price_lei_per_kwh": None,
        "opcom_margin_lei_per_kwh": None,
        "fixed_monthly_fee_lei": Decimal("0"),
        "variable_component_lei_per_kwh": Decimal("0"),
        "distribution_lei_per_kwh": Decimal("0"),
        "transport_lei_per_kwh": Decimal("0"),
        "other_regulated_lei_per_kwh": Decimal("0"),
        "vat_rate_percent": None,
        "settlement_method": "net_metering_15min",
        "settlement_interval_days": 30,
        "economic_calculation_disabled": False,
    }
    defaults.update(overrides)
    return TariffVersion(**defaults)


def test_fixed_tariff_uses_constant_marginal_price_independent_of_market(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("0.85"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("999")) == Decimal("0.85")
    assert svc.compute_effective_price_lei_per_kwh(v, None) == Decimal("0.85")  # nu are nevoie de pret OPCOM


def test_fixed_tariff_separates_marginal_cost_from_fixed_monthly_fee(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("0.85"), fixed_monthly_fee_lei=Decimal("25"))
    preview = svc.build_invoice_preview(v, None, Decimal("300"))
    assert preview["available"] is True
    assert preview["effective_price_lei_per_kwh"] == Decimal("0.85")  # costul marginal, fara abonament amestecat
    assert preview["energy_cost_lei"] == Decimal("255")  # 300 * 0.85
    assert preview["fixed_monthly_fee_lei"] == Decimal("25")
    assert preview["total_lei"] == Decimal("280")  # 255 + 25


def test_indexed_opcom_adds_margin_to_market_price(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.12"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("0.50")) == Decimal("0.62")


def test_indexed_opcom_negative_margin_allowed(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("-0.05"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("0.50")) == Decimal("0.45")


def test_indexed_opcom_without_market_price_returns_none_no_silent_fallback(db):
    """Criteriu explicit din issue #46/#51: lipsa pretului OPCOM blocheaza
    calculul exact, nu produce un fallback tacut (ex. 0 sau ultimul pret)."""
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.12"))
    assert svc.compute_effective_price_lei_per_kwh(v, None) is None


def test_economic_calculation_disabled_always_returns_none(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("0.85"), economic_calculation_disabled=True)
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("0.50")) is None


def test_none_tariff_version_returns_none(db):
    assert svc.compute_effective_price_lei_per_kwh(None, Decimal("0.50")) is None


def test_negative_opcom_price_flows_through_correctly(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.10"))
    assert svc.compute_effective_price_lei_per_kwh(v, Decimal("-0.30")) == Decimal("-0.20")


def test_all_network_and_regulated_components_sum_correctly(db):
    v = _version(
        fixed_price_lei_per_kwh=Decimal("0.50"),
        variable_component_lei_per_kwh=Decimal("0.01"),
        distribution_lei_per_kwh=Decimal("0.15"),
        transport_lei_per_kwh=Decimal("0.05"),
        other_regulated_lei_per_kwh=Decimal("0.02"),
    )
    # 0.50 + 0.01 + 0.15 + 0.05 + 0.02 = 0.73
    assert svc.compute_effective_price_lei_per_kwh(v, None) == Decimal("0.73")


def test_vat_applied_when_rate_set(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("1.00"), vat_rate_percent=Decimal("19"))
    assert svc.compute_effective_price_lei_per_kwh(v, None) == Decimal("1.19")


def test_vat_none_means_not_included_not_zero_percent(db):
    """None (implicit) si 0% trebuie sa produca rezultate DIFERITE conceptual
    -- desi numeric ambele lasa subtotalul neschimbat, `vat_rate_percent`
    ramane vizibil None in rezultat (nu e transformat tacit in 0)."""
    v_none = _version(fixed_price_lei_per_kwh=Decimal("1.00"), vat_rate_percent=None)
    v_zero = _version(fixed_price_lei_per_kwh=Decimal("1.00"), vat_rate_percent=Decimal("0"))
    assert svc.compute_effective_price_lei_per_kwh(v_none, None) == Decimal("1.00")
    assert svc.compute_effective_price_lei_per_kwh(v_zero, None) == Decimal("1.00")

    preview_none = svc.build_invoice_preview(v_none, None, Decimal("100"))
    preview_zero = svc.build_invoice_preview(v_zero, None, Decimal("100"))
    assert preview_none["vat_rate_percent"] is None
    assert preview_zero["vat_rate_percent"] == Decimal("0")


def test_vat_applied_to_fixed_monthly_fee_separately_in_preview(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("1.00"), fixed_monthly_fee_lei=Decimal("20"), vat_rate_percent=Decimal("19"))
    preview = svc.build_invoice_preview(v, None, Decimal("100"))
    assert preview["fixed_monthly_fee_with_vat_lei"] == Decimal("23.80")  # 20 * 1.19
    assert preview["energy_cost_lei"] == Decimal("119.00")  # 100 * 1.19
    assert preview["total_lei"] == Decimal("142.80")  # 119.00 + 23.80


def test_invoice_preview_unavailable_when_price_missing_gives_reason(db):
    v = _version(opcom_margin_lei_per_kwh=Decimal("0.10"))
    preview = svc.build_invoice_preview(v, None, Decimal("300"))
    assert preview["available"] is False
    assert "OPCOM" in preview["reason"]


def test_invoice_preview_unavailable_when_calculation_disabled(db):
    v = _version(fixed_price_lei_per_kwh=Decimal("1.00"), economic_calculation_disabled=True, limitation_note="test")
    preview = svc.build_invoice_preview(v, None, Decimal("300"))
    assert preview["available"] is False
    assert "dezactivat" in preview["reason"]


def test_add_tariff_version_persists_new_components(db):
    from tests.factories import make_org, make_station, make_user

    user = make_user(db, email="tariff-components@test.local")
    org = make_org(db, "Tariff Components Org")
    station = make_station(db, org, user, name="TC Station")
    db.commit()

    tariff = svc.get_or_create_tariff(db, station, "import", "fixed", "Test Fixed")
    version = svc.add_tariff_version(
        db, tariff, valid_from=datetime.now(UTC), fixed_price_lei_per_kwh=Decimal("1.00"),
        opcom_margin_lei_per_kwh=None, fixed_monthly_fee_lei=Decimal("0"), variable_component_lei_per_kwh=Decimal("0"),
        settlement_method="net_metering_15min", settlement_interval_days=30,
        distribution_lei_per_kwh=Decimal("0.12"), transport_lei_per_kwh=Decimal("0.03"),
        other_regulated_lei_per_kwh=Decimal("0.01"), vat_rate_percent=Decimal("19"),
    )
    db.commit()

    assert version.distribution_lei_per_kwh == Decimal("0.12")
    assert version.transport_lei_per_kwh == Decimal("0.03")
    assert version.other_regulated_lei_per_kwh == Decimal("0.01")
    assert version.vat_rate_percent == Decimal("19")
