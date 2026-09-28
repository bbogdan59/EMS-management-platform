from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.romanian_tariff import (
    COMPONENTS,
    INVOICE_EXAMPLE,
    RomanianTariffInput,
    RomanianTariffPreview,
    effective_instant,
)
from app.services.romanian_tariff_service import monthly_preview, prices


def config(**changes):
    return RomanianTariffInput.model_validate(
        {**INVOICE_EXAMPLE, "import_vat": "21", "export_vat": "0", **changes}
    )


def test_invoice_example_preserves_all_digits_and_independent_vat():
    rates = prices(config())
    assert rates["import_net"] == Decimal("0.95646320")
    assert rates["import_gross"] == Decimal("1.1573204720")
    assert rates["export_net"] == rates["export_gross"] == Decimal("0.446370")
    assert prices(config(export_vat="21"))["export_gross"] == Decimal("0.54010770")


def test_tg_already_included_is_not_added_twice_to_import():
    separate, included = prices(config()), prices(config(tg_in_active=True))
    assert separate["import_net"] - included["import_net"] == Decimal("0.003630")
    assert separate["export_net"] == included["export_net"]


@pytest.mark.parametrize(
    "import_kwh,export_kwh,matched,surplus,payable",
    [
        ("100", "100", "100", "0", "71.09504720"),
        ("300", "200", "200", "0", "257.92214160"),
        ("100", "300", "100", "200", "71.09504720"),
        ("0", "100", "0", "100", "0"),
    ],
)
def test_monthly_active_energy_netting_keeps_full_import_charges(
    import_kwh, export_kwh, matched, surplus, payable
):
    result = monthly_preview(config(), Decimal(import_kwh), Decimal(export_kwh))
    assert result["matched_kwh"] == Decimal(matched)
    assert result["surplus_kwh"] == Decimal(surplus)
    assert result["payable"] == Decimal(payable)
    assert result["active_credit"] == Decimal(matched) * Decimal("0.45")
    assert result["tg_retained"] == Decimal(matched) * Decimal("0.00363")
    distribution = next(r for r in result["rows"] if r["label"] == "Distributie")
    assert distribution["quantity"] == Decimal(import_kwh)
    assert distribution["value"] == Decimal(import_kwh) * Decimal("0.35534")


def test_zero_is_valid_and_missing_quantity_is_unknown():
    free = config(
        **dict.fromkeys((key for key, _ in COMPONENTS), "0"), import_vat="0", export_vat="0"
    )
    assert monthly_preview(free, Decimal(0), Decimal(0))["payable"] == 0
    assert not monthly_preview(free, None, Decimal(0))["available"]
    assert not monthly_preview(free, Decimal(0), None)["available"]
    assert "payable" not in monthly_preview(free, None, Decimal(0))


def test_fee_remains_payable_with_zero_energy_and_cannot_be_compensated():
    result = monthly_preview(config(monthly_fee="10"), Decimal(0), Decimal(500))
    assert result["payable"] == Decimal("12.10")
    assert result["surplus_credit_net"] == Decimal("223.18500")


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "-0.01", "0.123456789", "10000"])
def test_bill_rates_reject_unknown_nonfinite_negative_and_overprecision(value):
    with pytest.raises(ValidationError):
        config(cfd=value)


def test_decimal_comma_and_export_price_validation():
    assert config(green_certificates="0,07401920").green_certificates == Decimal("0.07401920")
    with pytest.raises(ValidationError, match="TG"):
        config(active_energy="0", tg="0.00363")
    with pytest.raises(ValidationError):
        config(import_vat="101")
    with pytest.raises(ValidationError):
        RomanianTariffPreview(tariff=config(), import_kwh="NaN", export_kwh="0")


@pytest.mark.parametrize(
    "local,expected",
    [
        ("2024-02-29T00:00", "2024-02-28T22:00:00+00:00"),
        ("2026-03-29T02:30", "2026-03-29T00:30:00+00:00"),
        ("2026-03-29T04:30", "2026-03-29T01:30:00+00:00"),
        ("2026-10-25T04:30", "2026-10-25T02:30:00+00:00"),
    ],
)
def test_contract_effective_dates_respect_station_calendar(local, expected):
    assert effective_instant(local, "Europe/Bucharest", datetime.now(UTC)).isoformat() == expected


@pytest.mark.parametrize("local", ["2026-03-29T03:30", "2026-10-25T03:30"])
def test_nonexistent_and_ambiguous_contract_times_are_rejected(local):
    with pytest.raises(ValueError, match="ambigua"):
        effective_instant(local, "Europe/Bucharest", datetime.now(UTC))
