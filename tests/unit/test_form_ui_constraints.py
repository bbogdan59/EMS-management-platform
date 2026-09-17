from __future__ import annotations

from pathlib import Path


def test_catalog_specs_json_textareas_have_enough_rows():
    template = Path("app/web/templates/admin/catalog.html").read_text()

    assert template.count('name="specs_json" rows="5"') == 2
    assert 'name="specs_json" rows="2"' not in template


def test_tariff_form_constrains_settlement_fields_to_supported_values():
    template = Path("app/web/templates/stations/tariffs.html").read_text()

    assert '<select class="input" name="settlement_method">' in template
    assert '<option value="net_metering_15min">net_metering_15min</option>' in template
    assert '<input class="input" type="number" name="settlement_interval_days" value="30" min="1" />' in template
    assert 'name="settlement_method" value="net_metering_15min"' not in template


def test_station_power_fields_use_same_step_and_min_as_creation_forms():
    config = Path("app/web/templates/stations/config.html").read_text()
    org_detail = Path("app/web/templates/organizations/detail.html").read_text()
    setup = Path("app/web/templates/organizations/setup_station.html").read_text()

    for template in [config, org_detail, setup]:
        assert 'numeric_field("pv_installed_power_kw", "Putere PV instalata", unit="kW", step="0.01", min=0.01, required=True)' in template or 'numeric_field("pv_installed_power_kw", "Putere PV instalata", value=config.pv_installed_power_kw if config, unit="kW", step="0.01", min=0.01, required=True)' in template
        assert 'numeric_field("inverter_power_kw", "Putere invertor", unit="kW", step="0.01", min=0.01, required=True)' in template or 'numeric_field("inverter_power_kw", "Putere invertor", value=config.inverter_power_kw if config, unit="kW", step="0.01", min=0.01, required=True)' in template


def test_device_activation_copy_only_promises_device_code():
    template = Path("app/web/templates/stations/devices.html").read_text()

    assert "Device Code-ul de pe eticheta sigilata" in template
    assert "ori serialul dispozitivului" not in template


def test_destructive_device_confirm_accepts_serial_or_installation_uuid_visibly():
    template = Path("app/web/templates/admin/devices_assigned.html").read_text()

    assert template.count('placeholder="confirma serialul sau UUID-ul de instalare"') == 2
    assert template.count("min-w-[24rem]") == 2
    assert 'style="width: 12rem"' not in template
    assert 'placeholder="confirma serialul"' not in template
