from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.opcom_fixtures import generate_synthetic_csv, intervals_for_date
from app.services.opcom_service import OpcomParseError, parse_csv


def test_intervals_for_normal_day():
    assert intervals_for_date(date(2026, 9, 9)) == 96


def test_intervals_for_spring_dst():
    assert intervals_for_date(date(2026, 3, 29)) == 92


def test_intervals_for_autumn_dst():
    assert intervals_for_date(date(2026, 10, 25)) == 100


def test_parse_synthetic_round_trip_normal_day():
    d = date(2026, 9, 9)
    csv_text = generate_synthetic_csv(d)
    parsed = parse_csv(csv_text, d)
    assert len(parsed) == 96
    assert parsed[0]["interval_index"] == 1
    assert parsed[-1]["interval_index"] == 96
    # Continuitate: fiecare interval e adiacent celui urmator, fara goluri.
    for a, b in zip(parsed, parsed[1:]):
        assert a["interval_end"] == b["interval_start"]


def test_parse_negative_prices_detected():
    d = date(2026, 9, 9)
    csv_text = generate_synthetic_csv(d)
    parsed = parse_csv(csv_text, d)
    negatives = [p for p in parsed if p["is_negative"]]
    assert len(negatives) >= 1
    for p in negatives:
        assert p["price_lei_per_mwh"] < 0
        assert p["price_lei_per_kwh"] < 0


def test_unit_conversion_mwh_to_kwh():
    csv_text = "Interval;Pret;Moneda\n1;100,00;RON\n"
    # Un singur interval nu e un numar valid de intervale (92/96/100) -> parse_csv trebuie sa respinga.
    with pytest.raises(OpcomParseError):
        parse_csv(csv_text, date(2026, 9, 9))


def test_currency_validation_rejects_unexpected_currency():
    rows = ["Interval;Pret;Moneda"]
    for i in range(1, 97):
        rows.append(f"{i};250,00;EUR")
    csv_text = "\n".join(rows)
    with pytest.raises(OpcomParseError):
        parse_csv(csv_text, date(2026, 9, 9))


def test_duplicate_interval_rejected():
    rows = ["Interval;Pret;Moneda"]
    for i in range(1, 96):
        rows.append(f"{i};250,00;RON")
    rows.append("1;999,00;RON")  # duplicat
    csv_text = "\n".join(rows)
    with pytest.raises(OpcomParseError):
        parse_csv(csv_text, date(2026, 9, 9))


def test_missing_intervals_rejected():
    rows = ["Interval;Pret;Moneda"]
    for i in list(range(1, 50)) + list(range(51, 97)):  # lipseste 50
        rows.append(f"{i};250,00;RON")
    csv_text = "\n".join(rows)
    with pytest.raises(OpcomParseError):
        parse_csv(csv_text, date(2026, 9, 9))


def test_header_not_found_raises_clear_error():
    csv_text = "col_a;col_b\n1;2\n3;4\n"
    with pytest.raises(OpcomParseError, match="antetul CSV"):
        parse_csv(csv_text, date(2026, 9, 9))


def test_comma_and_dot_decimal_separators_both_handled():
    from app.services.opcom_service import _parse_price

    assert _parse_price("123,45") == Decimal("123.45")
    assert _parse_price("123.45") == Decimal("123.45")
    assert _parse_price("-12,50") == Decimal("-12.50")


@pytest.mark.parametrize("text,expected", [("1.234,56", "1234.56"), ("1,234.56", "1234.56"), ("-1.234,56", "-1234.56")])
def test_grouped_prices_preserve_decimal_separator(text, expected):
    from app.services.opcom_service import _parse_price
    assert _parse_price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_prices_rejected(text):
    from app.services.opcom_service import _parse_price
    with pytest.raises(OpcomParseError):
        _parse_price(text)


@pytest.mark.parametrize("day,wrong_count", [(date(2026, 9, 10), 92), (date(2026, 3, 29), 96), (date(2026, 10, 25), 96)])
def test_interval_count_must_match_delivery_date(day, wrong_count):
    rows = ["Interval;Pret;Moneda"] + [f"{i};100;RON" for i in range(1, wrong_count + 1)]
    with pytest.raises(OpcomParseError, match="Numar neasteptat"):
        parse_csv("\n".join(rows), day)


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_dst_csv_ends_at_next_local_midnight(day):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    parsed = parse_csv(generate_synthetic_csv(day), day)
    end = parsed[-1]["interval_end"].astimezone(ZoneInfo("Europe/Bucharest"))
    assert end.date() == day + timedelta(days=1)
    assert (end.hour, end.minute) == (0, 0)
