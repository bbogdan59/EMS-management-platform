from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from app.services.opcom_fixtures import generate_synthetic_csv, intervals_for_date
from app.services.opcom_service import OpcomParseError, build_source_url, parse_csv

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


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
    for a, b in pairwise(parsed):
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


def _rows_with_resolution(n: int, resolution: str, delimiter: str = ";") -> str:
    header = delimiter.join(["Interval", "Pret", "Moneda", "Rezolutie"])
    lines = [header]
    for i in range(1, n + 1):
        lines.append(delimiter.join([str(i), "250,00", "RON", resolution]))
    return "\n".join(lines)


def test_parses_historical_hourly_resolution():
    """Anii istorici sunt publicati de OPCOM la rezolutie ORARA (PT60M,
    24 intervale/zi) -- inainte de acest fix, parserul presupunea orbeste
    15 minute si respingea aceste zile (numar de intervale neasteptat)."""
    d = date(2026, 9, 9)
    csv_text = _rows_with_resolution(24, "PT60M")

    parsed = parse_csv(csv_text, d)

    assert len(parsed) == 24
    assert parsed[0]["interval_index"] == 1
    assert parsed[-1]["interval_index"] == 24
    for a, b in pairwise(parsed):
        assert a["interval_end"] == b["interval_start"]
        assert b["interval_start"] - a["interval_start"] == timedelta(hours=1)
    assert parsed[0]["interval_end"] - parsed[0]["interval_start"] == timedelta(hours=1)


def test_parses_current_year_30min_resolution():
    """Anul curent alterneaza intre PT30M (48 intervale/zi) si PT15M."""
    d = date(2026, 9, 9)
    csv_text = _rows_with_resolution(48, "PT30M")

    parsed = parse_csv(csv_text, d)

    assert len(parsed) == 48
    for a, b in pairwise(parsed):
        assert a["interval_end"] == b["interval_start"]
        assert b["interval_start"] - a["interval_start"] == timedelta(minutes=30)


def test_pt1h_alias_treated_as_60_minutes():
    d = date(2026, 9, 9)
    csv_text = _rows_with_resolution(24, "PT1H")

    parsed = parse_csv(csv_text, d)

    assert len(parsed) == 24
    assert parsed[0]["interval_end"] - parsed[0]["interval_start"] == timedelta(hours=1)


def test_historical_source_url_requests_hourly_resolution():
    url = build_source_url(date(2024, 9, 14), today_local=date(2026, 9, 15))

    assert url.endswith("/14/09/2024/ro?resolution=60")


def test_recent_source_url_requests_15_minute_resolution():
    url = build_source_url(date(2026, 9, 14), today_local=date(2026, 9, 15))

    assert url.endswith("/14/09/2026/ro?resolution=15")


def test_hourly_resolution_inferred_when_column_absent():
    d = date(2024, 9, 9)
    rows = ["Interval;Pret;Moneda"]
    for i in range(1, 25):
        rows.append(f"{i};250,00;RON")

    parsed = parse_csv("\n".join(rows), d)

    assert len(parsed) == 24
    assert parsed[0]["interval_end"] - parsed[0]["interval_start"] == timedelta(hours=1)


def test_resolution_column_absent_defaults_to_15_minutes():
    """CSV-urile simple (fara coloana Rezolutie) trebuie sa se comporte
    neschimbat fata de inainte -- implicit 15 minute."""
    d = date(2026, 9, 9)
    csv_text = generate_synthetic_csv(d)  # nu are coloana Rezolutie

    parsed = parse_csv(csv_text, d)

    assert len(parsed) == 96
    assert parsed[0]["interval_end"] - parsed[0]["interval_start"] == timedelta(minutes=15)


def test_mixed_resolution_in_same_csv_rejected():
    d = date(2026, 9, 9)
    rows = ["Interval;Pret;Moneda;Rezolutie"]
    for i in range(1, 24):
        rows.append(f"{i};250,00;RON;PT60M")
    rows.append("24;250,00;RON;PT30M")  # rezolutie diferita in acelasi fisier
    csv_text = "\n".join(rows)

    with pytest.raises(OpcomParseError, match="rezolutii diferite"):
        parse_csv(csv_text, d)


def test_unsupported_resolution_rejected():
    d = date(2026, 9, 9)
    csv_text = _rows_with_resolution(288, "PT5M")  # rezolutie neacceptata

    with pytest.raises(OpcomParseError, match="nesuportata"):
        parse_csv(csv_text, d)


@pytest.mark.parametrize("day,expected_count", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_day_with_hourly_resolution_has_correct_count(day, expected_count):
    """Validarea numarului de intervale asteptat langa schimbarea orei trebuie
    sa se generalizeze corect la orice rezolutie, nu doar la 15 minute (vezi
    `test_intervals_for_spring_dst`/`test_intervals_for_autumn_dst` pentru
    echivalentul la 15 minute)."""
    csv_text = _rows_with_resolution(expected_count, "PT60M")

    parsed = parse_csv(csv_text, day)

    assert len(parsed) == expected_count
    from zoneinfo import ZoneInfo

    end = parsed[-1]["interval_end"].astimezone(ZoneInfo("Europe/Bucharest"))
    assert end.date() == day + timedelta(days=1)
    assert (end.hour, end.minute) == (0, 0)


def test_parses_real_opcom_export_sample():
    """Regressie pentru un export CSV real descarcat de pe opcom.ro
    (rezolutie 15 minute): virgula ca delimitator, fiecare camp incadrat in
    ghilimele duble (RFC4180), un titlu + un tabel sumar (medii Base/Peak/
    Off-Peak) inaintea tabelului detaliat, iar coloana de pret se numeste
    "Pret de Inchidere a Pietei [lei/MWh]" -- nu doar "Pret". Fisierul de test
    e o copie neschimbata a exportului real."""
    csv_text = (FIXTURES_DIR / "opcom_real_sample_pt15m_2026-09-12.csv").read_text(encoding="utf-8-sig")
    d = date(2026, 9, 12)

    parsed = parse_csv(csv_text, d)

    assert len(parsed) == 96
    assert parsed[0]["interval_index"] == 1
    assert parsed[0]["price_lei_per_mwh"] == Decimal("1233.84")
    assert parsed[1]["price_lei_per_mwh"] == Decimal("1229.63")
    assert parsed[-1]["interval_index"] == 96
    assert parsed[-1]["price_lei_per_mwh"] == Decimal("1000.08")
    for p in parsed:
        assert p["currency"] == "RON"
    for a, b in pairwise(parsed):
        assert a["interval_end"] == b["interval_start"]
