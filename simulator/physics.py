"""Model fizic simplu si determinist pentru simulatorul de protocol.

Acesta NU este cod pentru Raspberry Pi/ESP32 si nu vorbeste Modbus/RS485 --
este un client HTTP care produce date de telemetrie plauzibile pentru a
exercita API-ul public al platformei (asociere, telemetrie, planuri, comenzi).

Reproductibilitate: toate variatiile aleatoare (nori, zgomot de consum)
folosesc `random.Random(seed)` -- aceeasi combinatie (seed, statie, zi)
produce intotdeauna aceleasi date.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass
class StationProfile:
    label: str
    timezone: str
    pv_kwp: float
    inverter_kw: float
    battery_capacity_kwh: float
    battery_max_charge_kw: float
    battery_max_discharge_kw: float
    charge_eff: float = 0.95
    discharge_eff: float = 0.95
    min_reserve_soc: float = 15.0
    max_normal_soc: float = 95.0
    allow_grid_charge: bool = False
    allow_battery_export: bool = False
    ev_enabled: bool = False
    ev_battery_kwh: float = 0.0
    ev_max_charge_kw: float = 0.0
    seed: int = 0
    load_base_kw: float = 0.5
    load_peak_kw: float = 2.2


@dataclass
class TelemetrySample:
    measured_at: datetime
    pv_power_w: float
    load_power_w: float
    battery_power_w: float
    grid_power_w: float
    battery_soc_percent: float
    ev_connected: bool
    ev_power_w: float
    next_soc_kwh: float


def _cloud_factor(rng: random.Random, day_seed_key: str) -> float:
    day_rng = random.Random(day_seed_key)
    # Zi predominant insorita, cu ocazional nori (0.3-0.7 din productia clara).
    return 1.0 if day_rng.random() > 0.25 else day_rng.uniform(0.35, 0.75)


def _pv_power_kw(profile: StationProfile, local_dt: datetime, cloud_factor: float) -> float:
    hour = local_dt.hour + local_dt.minute / 60
    if hour < 6 or hour > 20:
        return 0.0
    solar = math.sin((hour - 6) / 14 * math.pi)
    return max(0.0, profile.pv_kwp * solar * cloud_factor)


def _load_power_kw(profile: StationProfile, local_dt: datetime, rng: random.Random) -> float:
    hour = local_dt.hour + local_dt.minute / 60
    morning = math.exp(-((hour - 7.5) ** 2) / 3)
    evening = math.exp(-((hour - 20) ** 2) / 4)
    shape = 0.3 + 0.5 * morning + 0.8 * evening
    noise = rng.uniform(-0.1, 0.15)
    return max(0.05, profile.load_base_kw + (profile.load_peak_kw - profile.load_base_kw) * shape + noise)


def _ev_state(profile: StationProfile, local_dt: datetime) -> tuple[bool, float]:
    if not profile.ev_enabled:
        return False, 0.0
    hour = local_dt.hour
    connected = hour >= 19 or hour < 7
    if not connected:
        return False, 0.0
    charging_hours_left = (24 - hour) if hour >= 19 else (7 - hour)
    power = profile.ev_max_charge_kw if charging_hours_left > 1 else profile.ev_max_charge_kw * 0.4
    return True, power


def simulate_tick(
    profile: StationProfile,
    measured_at_utc: datetime,
    prev_soc_kwh: float,
    dt_hours: float,
    override_battery_power_kw: float | None = None,
) -> TelemetrySample:
    tz = ZoneInfo(profile.timezone)
    local_dt = measured_at_utc.astimezone(tz)
    rng = random.Random(f"{profile.seed}:{profile.label}:{measured_at_utc.isoformat()}")

    cloud_factor = _cloud_factor(rng, f"{profile.seed}:{profile.label}:{local_dt.date().isoformat()}")
    pv_kw = _pv_power_kw(profile, local_dt, cloud_factor)
    load_kw = _load_power_kw(profile, local_dt, rng)
    ev_connected, ev_kw = _ev_state(profile, local_dt)

    soc_percent = prev_soc_kwh / profile.battery_capacity_kwh * 100 if profile.battery_capacity_kwh else 0

    if override_battery_power_kw is not None:
        batt_kw = override_battery_power_kw
    else:
        surplus = pv_kw - load_kw - ev_kw
        if surplus > 0.05 and soc_percent < profile.max_normal_soc:
            batt_kw = min(surplus, profile.battery_max_charge_kw)
        elif surplus < -0.05 and soc_percent > profile.min_reserve_soc:
            batt_kw = max(surplus, -profile.battery_max_discharge_kw)
        else:
            batt_kw = 0.0
        if not profile.allow_grid_charge and batt_kw > pv_kw:
            batt_kw = max(0.0, pv_kw)
        if not profile.allow_battery_export and batt_kw < 0 and abs(batt_kw) > (load_kw + ev_kw):
            batt_kw = -(load_kw + ev_kw)

    if batt_kw >= 0:
        delta_kwh = batt_kw * profile.charge_eff * dt_hours
    else:
        delta_kwh = batt_kw / profile.discharge_eff * dt_hours
    next_soc_kwh = min(max(prev_soc_kwh + delta_kwh, 0.0), profile.battery_capacity_kwh)

    grid_kw = load_kw + ev_kw + batt_kw - pv_kw

    return TelemetrySample(
        measured_at=measured_at_utc,
        pv_power_w=round(pv_kw * 1000, 1),
        load_power_w=round(load_kw * 1000, 1),
        battery_power_w=round(batt_kw * 1000, 1),
        grid_power_w=round(grid_kw * 1000, 1),
        battery_soc_percent=round(next_soc_kwh / profile.battery_capacity_kwh * 100, 2) if profile.battery_capacity_kwh else 0,
        ev_connected=ev_connected,
        ev_power_w=round(ev_kw * 1000, 1),
        next_soc_kwh=next_soc_kwh,
    )
