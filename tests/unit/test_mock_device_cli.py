"""Testeaza doar partile pure ale CLI-ului cu un singur dispozitiv
(`simulator/run_mock_device.py`): parsarea argumentelor si construirea
`StationProfile`-ului rezultat. Nu necesita un server live -- vezi
docstring-ul modulului pentru fluxul manual complet impotriva unui server
real (necesita un cod de asociere generat din UI)."""
from __future__ import annotations

import pytest

from simulator.run_mock_device import build_arg_parser, build_profile, ensure_claimed
from simulator.state import SimulatorState


def test_defaults_produce_reasonable_profile():
    args = build_arg_parser().parse_args(["--claim-code", "ABCD1234"])
    profile = build_profile(args)

    assert profile.label == "mock-device"
    assert profile.pv_kwp == 5.0
    assert profile.inverter_kw == 5.0
    assert profile.battery_capacity_kwh == 10.0
    # Implicit: jumatate din capacitate cand nu e specificat explicit.
    assert profile.battery_max_charge_kw == 5.0
    assert profile.battery_max_discharge_kw == 5.0
    assert profile.min_reserve_soc == 15.0
    assert profile.max_normal_soc == 95.0
    assert profile.allow_grid_charge is False
    assert profile.ev_enabled is False
    assert profile.timezone == "Europe/Bucharest"


def test_explicit_battery_power_overrides_half_capacity_default():
    args = build_arg_parser().parse_args(
        [
            "--claim-code", "ABCD1234",
            "--battery-kwh", "20",
            "--battery-max-charge-kw", "3",
            "--battery-max-discharge-kw", "4",
        ]
    )
    profile = build_profile(args)

    assert profile.battery_capacity_kwh == 20.0
    assert profile.battery_max_charge_kw == 3.0
    assert profile.battery_max_discharge_kw == 4.0


def test_ev_and_custom_params_parsed():
    args = build_arg_parser().parse_args(
        [
            "--claim-code", "ABCD1234",
            "--label", "test-station",
            "--pv-kwp", "8.5",
            "--inverter-kw", "6",
            "--battery-kwh", "15",
            "--ev-enabled",
            "--ev-battery-kwh", "50",
            "--ev-max-charge-kw", "7.4",
            "--allow-grid-charge",
            "--allow-battery-export",
            "--seed", "7",
        ]
    )
    profile = build_profile(args)

    assert profile.label == "test-station"
    assert profile.pv_kwp == 8.5
    assert profile.inverter_kw == 6.0
    assert profile.battery_capacity_kwh == 15.0
    assert profile.ev_enabled is True
    assert profile.ev_battery_kwh == 50.0
    assert profile.ev_max_charge_kw == 7.4
    assert profile.allow_grid_charge is True
    assert profile.allow_battery_export is True
    assert profile.seed == 7


def test_claim_code_not_required_by_parser_itself():
    """--claim-code e opțional la nivel de parser (poate lipsi la rulari
    ulterioare cu stare deja salvata) -- validarea "e necesar la prima
    rulare" se face explicit in `ensure_claimed`, nu in parser."""
    args = build_arg_parser().parse_args([])
    assert args.claim_code is None


def test_ensure_claimed_requires_claim_code_when_no_saved_state(tmp_path):
    state = SimulatorState(str(tmp_path))
    with pytest.raises(SystemExit):
        ensure_claimed(client=None, state=state, label="no-such-label", claim_code=None)


def test_ensure_claimed_reuses_saved_credentials_without_claim_code(tmp_path):
    state = SimulatorState(str(tmp_path))
    state.save("existing-device", {"device_id": "dev-1", "station_id": "st-1", "secret": "s3cr3t"})

    calls = []

    class _FakeClient:
        def set_credentials(self, device_id, secret):
            calls.append((device_id, secret))

    saved = ensure_claimed(client=_FakeClient(), state=state, label="existing-device", claim_code=None)

    assert saved["device_id"] == "dev-1"
    assert calls == [("dev-1", "s3cr3t")]
