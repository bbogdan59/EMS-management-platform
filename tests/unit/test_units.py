from __future__ import annotations

from decimal import Decimal

from app.core.units import KWH_PER_MWH, kwh_to_mwh, mwh_to_kwh


def test_kwh_per_mwh_is_exactly_one_thousand():
    assert Decimal(1000) == KWH_PER_MWH


def test_mwh_to_kwh_divides_by_one_thousand():
    assert mwh_to_kwh(Decimal("500")) == Decimal("0.5")
    assert mwh_to_kwh(Decimal("1000")) == Decimal("1")


def test_kwh_to_mwh_multiplies_by_one_thousand():
    assert kwh_to_mwh(Decimal("0.5")) == Decimal("500")
    assert kwh_to_mwh(Decimal("1")) == Decimal("1000")


def test_round_trip_conversion_is_lossless():
    original = Decimal("342.756")
    assert kwh_to_mwh(mwh_to_kwh(original)) == original
    assert mwh_to_kwh(kwh_to_mwh(original)) == original


def test_negative_price_conversion_preserves_sign():
    assert mwh_to_kwh(Decimal("-250")) == Decimal("-0.25")
    assert kwh_to_mwh(Decimal("-0.25")) == Decimal("-250")


def test_zero_price_conversion():
    assert mwh_to_kwh(Decimal("0")) == Decimal("0")
    assert kwh_to_mwh(Decimal("0")) == Decimal("0")
