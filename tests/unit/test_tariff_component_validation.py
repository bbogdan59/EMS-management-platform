from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.tariff_service import _validate_version_components


def _valid_components(**overrides):
    data = {
        "fixed_price_lei_per_kwh": Decimal("0.85"),
        "fixed_monthly_fee_lei": Decimal("20"),
        "variable_component_lei_per_kwh": Decimal("0"),
        "distribution_lei_per_kwh": Decimal("0.12"),
        "transport_lei_per_kwh": Decimal("0.03"),
        "other_regulated_lei_per_kwh": Decimal("0.01"),
        "vat_rate_percent": Decimal("19"),
        "settlement_interval_days": 30,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "field",
    [
        "fixed_price_lei_per_kwh",
        "fixed_monthly_fee_lei",
        "variable_component_lei_per_kwh",
        "distribution_lei_per_kwh",
        "transport_lei_per_kwh",
        "other_regulated_lei_per_kwh",
        "vat_rate_percent",
    ],
)
def test_tariff_components_reject_negative_cost_fields(field):
    with pytest.raises(ValueError, match=field):
        _validate_version_components(**_valid_components(**{field: Decimal("-0.01")}))


def test_tariff_components_reject_implausible_vat_and_settlement_interval():
    with pytest.raises(ValueError, match="vat_rate_percent"):
        _validate_version_components(**_valid_components(vat_rate_percent=Decimal("100.01")))

    with pytest.raises(ValueError, match="settlement_interval_days"):
        _validate_version_components(**_valid_components(settlement_interval_days=0))


def test_tariff_components_allow_missing_vat_and_zero_cost_fields():
    _validate_version_components(
        **_valid_components(
            fixed_price_lei_per_kwh=Decimal("0"),
            fixed_monthly_fee_lei=Decimal("0"),
            distribution_lei_per_kwh=Decimal("0"),
            vat_rate_percent=None,
        )
    )
