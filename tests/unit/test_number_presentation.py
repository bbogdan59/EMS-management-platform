from decimal import Decimal

from app.web.templating import fmt_lei, fmt_lei_per_kwh, fmt_number, fmt_percent


def test_display_distinguishes_missing_zero_and_small_tariffs():
    assert fmt_number(None) == "-"
    assert fmt_number(Decimal("NaN")) == "-"
    assert fmt_number(Decimal("0")) == "0,00"
    assert fmt_lei_per_kwh(Decimal("0.00085")) == "0,00085 lei/kWh"
    assert fmt_lei(Decimal("-1234.56")) == "-1.234,56 lei"
    assert fmt_percent(None) == "-"
    assert fmt_percent(Decimal("0")) == "0%"
    assert fmt_percent(Decimal("0.985")) == "98%"


def test_display_preserves_decimal_precision_for_large_amounts():
    assert fmt_number(Decimal("9999999999999.99")) == "9.999.999.999.999,99"
