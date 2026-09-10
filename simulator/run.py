"""Orchestratorul simulatorului: proces separat, exclusiv software, care
vorbeste DOAR cu API-ul public /api/v1 al platformei (asociere, telemetrie,
planuri, comenzi). Nu este cod pentru Raspberry Pi/ESP32 si nu implementeaza
Modbus/RS485 -- e un simulator de PROTOCOL pentru a putea evalua platforma
fara echipamente.

Pentru fiecare statie din fisierul de seed (vezi scripts/seed_demo.py):
  1. Se asociaza (claim) o singura data, credentialele sunt persistate local.
  2. Se genereaza un istoric reproductibil (backfill) daca nu exista deja.
  3. Intra intr-o bucla "live": telemetrie in timp real, heartbeat, preluare
     plan/comenzi, confirmare/raportare executie -- cu scenarii configurabile
     de offline, date intarziate si comenzi respinse.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import threading
import time
from datetime import UTC, datetime, timedelta

from simulator.api_client import DeviceApiClient
from simulator.physics import StationProfile, simulate_tick
from simulator.state import SimulatorState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
logger = logging.getLogger("simulator")


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def load_profile(entry: dict) -> StationProfile:
    p = entry["profile"]
    return StationProfile(
        label=entry["station_key"],
        timezone=p.get("timezone", "Europe/Bucharest"),
        pv_kwp=p["pv_kwp"],
        inverter_kw=p["inverter_kw"],
        battery_capacity_kwh=p["battery_capacity_kwh"],
        battery_max_charge_kw=p["battery_max_charge_kw"],
        battery_max_discharge_kw=p["battery_max_discharge_kw"],
        min_reserve_soc=p.get("min_reserve_soc", 15.0),
        max_normal_soc=p.get("max_normal_soc", 95.0),
        allow_grid_charge=p.get("allow_grid_charge", False),
        allow_battery_export=p.get("allow_battery_export", False),
        ev_enabled=p.get("ev_enabled", False),
        ev_battery_kwh=p.get("ev_battery_kwh", 0.0),
        ev_max_charge_kw=p.get("ev_max_charge_kw", 0.0),
        seed=p.get("seed", 42),
        load_base_kw=p.get("load_base_kw", 0.5),
        load_peak_kw=p.get("load_peak_kw", 2.2),
    )


def ensure_claimed(client: DeviceApiClient, state: SimulatorState, entry: dict) -> dict:
    saved = state.load(entry["station_key"])
    if saved and saved.get("device_id") and saved.get("secret"):
        client.set_credentials(saved["device_id"], saved["secret"])
        return saved

    result = client.claim(entry["claim_code"], f"Simulator - {entry['station_key']}", {"model": "ems-simulator", "version": "1.0"})
    saved = {"device_id": result["device_id"], "station_id": result["station_id"], "secret": result["credential_secret"], "backfilled": False}
    state.save(entry["station_key"], saved)
    client.set_credentials(saved["device_id"], saved["secret"])
    logger.info("Dispozitiv asociat pentru %s (device_id=%s)", entry["station_key"], saved["device_id"])
    return saved


def run_backfill(client: DeviceApiClient, profile: StationProfile, days: int, end_time: datetime) -> float:
    logger.info("Backfill %d zile pentru %s (reproductibil, seed=%s)", days, profile.label, profile.seed)
    start = end_time - timedelta(days=days)
    soc_kwh = profile.battery_capacity_kwh * 0.5
    boot_id = f"backfill-{profile.seed}"
    sequence = 0
    batch: list[dict] = []
    t = start
    while t < end_time:
        sample = simulate_tick(profile, t, soc_kwh, dt_hours=0.25)
        soc_kwh = sample.next_soc_kwh
        sequence += 1
        batch.append(
            {
                "boot_id": boot_id,
                "sequence": sequence,
                "measured_at": t.isoformat(),
                "pv_power_w": sample.pv_power_w,
                "load_power_w": sample.load_power_w,
                "battery_power_w": sample.battery_power_w,
                "grid_power_w": sample.grid_power_w,
                "battery_soc_percent": sample.battery_soc_percent,
                "ev_connected": sample.ev_connected,
                "ev_power_w": sample.ev_power_w,
                "raw_payload": {"simulated": True, "scenario": "backfill"},
            }
        )
        if len(batch) >= 400:
            result = client.send_telemetry(batch)
            logger.info("%s backfill batch: %s", profile.label, result)
            batch = []
        t += timedelta(minutes=15)

    if batch:
        result = client.send_telemetry(batch)
        logger.info("%s backfill batch final: %s", profile.label, result)

    return soc_kwh


def live_loop(client: DeviceApiClient, profile: StationProfile, soc_kwh: float, scenarios: dict, stop_event: threading.Event) -> None:
    boot_id = f"live-{int(time.time())}"
    sequence = 0
    last_heartbeat = 0.0
    last_plan_check = 0.0
    accepted_plan_version: int | None = None
    rng = random.Random()
    pending_late: list[dict] = []

    while not stop_event.is_set():
        now = datetime.now(UTC)

        if rng.random() < scenarios["offline_probability"]:
            logger.info("%s: simulare OFFLINE pentru acest tick (nu se trimite nimic)", profile.label)
            time.sleep(scenarios["tick_seconds"])
            continue

        if time.time() - last_heartbeat > 60:
            try:
                hb = client.heartbeat(boot_id, "1.0.0-simulator", {"max_charge_w": profile.battery_max_charge_kw * 1000})
                last_heartbeat = time.time()
                logger.info("%s heartbeat: %s", profile.label, hb)
            except Exception as exc:
                logger.warning("%s heartbeat esuat: %s", profile.label, exc)

        override_kw = None
        try:
            if time.time() - last_plan_check > 30:
                plan = client.get_active_plan()
                last_plan_check = time.time()
                if plan.get("plan_id") and plan.get("status") == "published" and plan.get("version") != accepted_plan_version:
                    client.accept_plan(plan["version"])
                    accepted_plan_version = plan["version"]
                    logger.info("%s: plan v%s acceptat (mod %s)", profile.label, plan["version"], plan.get("execution_mode"))

                commands = client.list_pending_commands()
                for cmd in commands:
                    should_reject = rng.random() < scenarios["command_reject_probability"]
                    if should_reject:
                        client.ack_command(cmd["command_id"], "rejected", "Simulare: dispozitivul respinge comanda pentru testare.")
                        logger.info("%s: comanda %s RESPINSA (scenariu simulat)", profile.label, cmd["command_id"])
                        continue
                    client.ack_command(cmd["command_id"], "accepted")
                    if profile_execution_mode_allows_live(plan):
                        override_kw = cmd["parameters"].get("battery_power_kw")
                    client.report_result(cmd["command_id"], "executed", {"applied_battery_power_kw": override_kw})
                    logger.info("%s: comanda %s acceptata si executata (simulat)", profile.label, cmd["command_id"])
        except Exception as exc:
            logger.warning("%s: eroare la verificarea planului/comenzilor: %s", profile.label, exc)

        sample = simulate_tick(profile, now, soc_kwh, dt_hours=scenarios["tick_seconds"] / 3600, override_battery_power_kw=override_kw)
        soc_kwh = sample.next_soc_kwh
        sequence += 1
        item = {
            "boot_id": boot_id,
            "sequence": sequence,
            "measured_at": now.isoformat(),
            "pv_power_w": sample.pv_power_w,
            "load_power_w": sample.load_power_w,
            "battery_power_w": sample.battery_power_w,
            "grid_power_w": sample.grid_power_w,
            "battery_soc_percent": sample.battery_soc_percent,
            "ev_connected": sample.ev_connected,
            "ev_power_w": sample.ev_power_w,
            "raw_payload": {"simulated": True, "scenario": "live"},
        }

        if rng.random() < scenarios["late_data_probability"]:
            pending_late.append(item)
            logger.info("%s: telemetrie INTARZIATA intentionat (retinuta pentru urmatorul tick)", profile.label)
        else:
            to_send = [*pending_late, item]
            pending_late = []
            try:
                result = client.send_telemetry(to_send)
                logger.info("%s telemetrie: %s", profile.label, result)
            except Exception as exc:
                logger.warning("%s: eroare la trimiterea telemetriei: %s", profile.label, exc)

        time.sleep(scenarios["tick_seconds"])


def profile_execution_mode_allows_live(plan: dict | None) -> bool:
    return bool(plan and plan.get("execution_mode") == "live")


def run_station(entry: dict, api_base_url: str, state_dir: str, backfill_days: int, scenarios: dict, stop_event: threading.Event) -> None:
    client = DeviceApiClient(api_base_url)
    state = SimulatorState(state_dir)
    profile = load_profile(entry)

    saved = ensure_claimed(client, state, entry)
    soc_kwh = profile.battery_capacity_kwh * 0.5

    if not saved.get("backfilled"):
        soc_kwh = run_backfill(client, profile, backfill_days, datetime.now(UTC) - timedelta(minutes=15))
        saved["backfilled"] = True
        state.save(entry["station_key"], saved)

    live_loop(client, profile, soc_kwh, scenarios, stop_event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulator de protocol pentru platforma EMS (fara Raspberry Pi/ESP32/Modbus).")
    parser.add_argument("--seed-file", default=os.environ.get("SIMULATOR_SEED_FILE", "/data/simulator_seed.json"))
    parser.add_argument("--api-base-url", default=os.environ.get("SIMULATOR_API_BASE_URL", "http://localhost:8000/api/v1"))
    parser.add_argument("--state-dir", default=os.environ.get("SIMULATOR_STATE_DIR", "/data/simulator_state"))
    parser.add_argument("--backfill-days", type=int, default=_env_int("SIMULATOR_BACKFILL_DAYS", 14))
    args = parser.parse_args()

    with open(args.seed_file) as f:
        entries = json.load(f)

    scenarios = {
        "tick_seconds": _env_float("SIMULATOR_TICK_SECONDS", 20),
        "offline_probability": _env_float("SIMULATOR_OFFLINE_PROBABILITY", 0.02),
        "late_data_probability": _env_float("SIMULATOR_LATE_DATA_PROBABILITY", 0.05),
        "command_reject_probability": _env_float("SIMULATOR_COMMAND_REJECT_PROBABILITY", 0.1),
    }

    stop_event = threading.Event()
    threads = []
    for entry in entries:
        t = threading.Thread(
            target=run_station, args=(entry, args.api_base_url, args.state_dir, args.backfill_days, scenarios, stop_event),
            name=entry["station_key"], daemon=True,
        )
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Oprire simulator...")
        stop_event.set()


if __name__ == "__main__":
    main()
