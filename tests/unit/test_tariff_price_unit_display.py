"""Tarifele lei/kWh trebuie afisate cu unitatea explicita si fara sa piarda
precizia stocata (`Numeric(10, 5)`) -- vezi issue #114: `fixed_price_lei_per_kwh`
se afisa anterior prin `fmt_lei` (rotunjit la 2 zecimale, eticheta "lei" fara
"/kWh"), ceea ce facea o marja mica reala (ex. 0.00085 lei/kWh) sa para 0."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.web.templating import fmt_lei_per_kwh


def test_fmt_lei_per_kwh_preserves_five_decimals():
    assert fmt_lei_per_kwh(Decimal("0.68450")) == "0,68450 lei/kWh"


def test_fmt_lei_per_kwh_does_not_round_small_value_to_zero():
    assert fmt_lei_per_kwh(Decimal("0.00085")) == "0,00085 lei/kWh"


def test_fmt_lei_per_kwh_none_renders_dash():
    assert fmt_lei_per_kwh(None) == "-"


def test_tariffs_table_uses_lei_per_kwh_filter_for_every_component_column():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    for field in [
        "fixed_price_lei_per_kwh",
        "opcom_margin_lei_per_kwh",
        "distribution_lei_per_kwh",
        "transport_lei_per_kwh",
        "other_regulated_lei_per_kwh",
    ]:
        assert f"v.{field} | lei_per_kwh" in template


def test_tariffs_table_header_labels_lei_per_kwh_columns():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    for header in ["Pret fix (lei/kWh)", "Marja OPCOM (lei/kWh)", "Distributie (lei/kWh)", "Transport (lei/kWh)", "Alte taxe (lei/kWh)"]:
        assert header in template
