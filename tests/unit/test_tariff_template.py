from __future__ import annotations

from pathlib import Path


def test_tariff_template_preserves_explicit_zero_fixed_price():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    assert "v.fixed_price_lei_per_kwh is not none" in template
    assert "if v.fixed_price_lei_per_kwh else" not in template


def test_tariff_form_has_explainable_presets_and_live_preview():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    assert 'id="tariff-preset"' in template
    assert "Import fix: pret din contract + componente" in template
    assert "Import dinamic: OPCOM + marja + componente" in template
    assert "Export fix: pret propriu, separat de import" in template
    assert "Custom: formula neimplementata, calcul dezactivat" in template
    assert 'id="tariff-live-preview"' in template
    assert "Preview factura-exemplu inainte de salvare" in template
    assert "nu include reguli comerciale neverificate" in template


def test_tariff_live_preview_uses_same_component_shape_as_service():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    for field in [
        "fixed_price_lei_per_kwh",
        "opcom_margin_lei_per_kwh",
        "variable_component_lei_per_kwh",
        "distribution_lei_per_kwh",
        "transport_lei_per_kwh",
        "other_regulated_lei_per_kwh",
        "vat_rate_percent",
        "fixed_monthly_fee_lei",
    ]:
        assert f"form.elements.{field}" in template
    assert "marketPrice + num(margin)" in template
    assert "effective *= 1 + vatRate / 100" in template
