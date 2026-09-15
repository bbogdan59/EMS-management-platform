from __future__ import annotations

from pathlib import Path


def test_tariff_template_preserves_explicit_zero_fixed_price():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    assert "v.fixed_price_lei_per_kwh is not none" in template
    assert "if v.fixed_price_lei_per_kwh else" not in template
