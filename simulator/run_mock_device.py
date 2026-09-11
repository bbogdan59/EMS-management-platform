"""CLI simplu, cu UN SINGUR dispozitiv, pentru testarea platformei web fara
hardware fizic si fara fisierul de seed multi-statie cerut de `run.py`
(vezi `run.py` pentru orchestrarea completa, multi-statie, folosita de demo).

Reutilizeaza neschimbate `simulator/physics.py` (modelul fizic determinist)
si `simulator/api_client.py` (clientul HTTP peste API-ul public de
dispozitive) -- acest fisier e doar un al doilea entry point, mai simplu,
peste aceleasi module.

Pasi pentru un test rapid local:
  1. Porneste platforma (`./run_local.sh` sau echivalent).
  2. Creeaza o organizatie + o statie din UI (admin).
  3. Din pagina statiei, genereaza un cod de asociere pentru un dispozitiv nou.
  4. Ruleaza:
       python -m simulator.run_mock_device --claim-code ABCD1234 \
           --pv-kwp 5 --inverter-kw 5 --battery-kwh 10

     Toti parametrii fizici au valori implicite rezonabile -- poti rula si
     doar cu `--claim-code`. La rulari ulterioare (acelasi `--label`),
     `--claim-code` nu mai e necesar -- credentialele sunt reutilizate din
     fisierul de stare local.

Opreste cu Ctrl+C (SIGINT) -- iesire curata, fara proces zombie.

Trimite doar telemetrie live (fara backfill istoric) plus heartbeat, si
accepta automat orice plan publicat/comanda primita (fara scenarii de
haos -- offline/intarziere/respingere -- disponibile doar in `run.py`,
pentru testare de robustete, nu pentru un test rapid de fum)."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime

from simulator.api_client import DeviceApiClient
from simulator.physics import StationProfile, simulate_tick
from simulator.state import SimulatorState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("simulator.mock_device")


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def build_profile(args: argparse.Namespace) -> StationProfile:
    """Construieste un `StationProfile` din argumentele CLI -- pura, fara
    efecte secundare, testabila fara server live."""
    half_capacity = args.battery_kwh / 2
    return StationProfile(
        label=args.label,
        timezone=args.timezone,
        pv_kwp=args.pv_kwp,
        inverter_kw=args.inverter_kw,
        battery_capacity_kwh=args.battery_kwh,
        battery_max_charge_kw=args.battery_max_charge_kw if args.battery_max_charge_kw is not None else half_capacity,
        battery_max_discharge_kw=args.battery_max_discharge_kw if args.battery_max_discharge_kw is not None else half_capacity,
        min_reserve_soc=args.min_soc,
        max_normal_soc=args.max_soc,
        allow_grid_charge=args.allow_grid_charge,
        allow_battery_export=args.allow_battery_export,
        ev_enabled=args.ev_enabled,
        ev_battery_kwh=args.ev_battery_kwh,
        ev_max_charge_kw=args.ev_max_charge_kw,
        seed=args.seed,
        load_base_kw=args.load_base_kw,
        load_peak_kw=args.load_peak_kw,
    )


def ensure_claimed(client: DeviceApiClient, state: SimulatorState, label: str, claim_code: str | None) -> dict:
    saved = state.load(label)
    if saved and saved.get("device_id") and saved.get("secret"):
        client.set_credentials(saved["device_id"], saved["secret"])
        logger.info("Reutilizez credentialele existente pentru '%s' (device_id=%s)", label, saved["device_id"])
        return saved

    if not claim_code:
        raise SystemExit(
            f"--claim-code este necesar la prima rulare (nicio stare salvata pentru --label '{label}')."
        )

    result = client.claim(claim_code, f"Mock device - {label}", {"model": "ems-mock-device", "version": "1.0"})
    saved = {"device_id": result["device_id"], "station_id": result["station_id"], "secret": result["credential_secret"]}
    state.save(label, saved)
    client.set_credentials(saved["device_id"], saved["secret"])
    logger.info("Dispozitiv asociat pentru '%s' (device_id=%s, station_id=%s)", label, saved["device_id"], saved["station_id"])
    return saved


def _handle_plan_and_commands(client: DeviceApiClient, label: str) -> float | None:
    """Accepta automat orice plan publicat nou si orice comanda in asteptare
    (fara scenarii de respingere -- acesta e un dispozitiv "bine-crescut",
    nu un test de robustete). Returneaza puterea bateriei ceruta explicit de
    ultima comanda acceptata (daca planul e live), altfel None."""
    override_kw = None
    try:
        plan = client.get_active_plan()
        commands = client.list_pending_commands()
        for cmd in commands:
            client.ack_command(cmd["command_id"], "accepted")
            if plan.get("execution_mode") == "live":
                override_kw = cmd["parameters"].get("battery_power_kw")
            client.report_result(cmd["command_id"], "executed", {"applied_battery_power_kw": override_kw})
            logger.info("%s: comanda %s acceptata si executata", label, cmd["command_id"])
    except Exception as exc:
        logger.warning("%s: eroare la verificarea planului/comenzilor: %s", label, exc)
    return override_kw


def live_loop(client: DeviceApiClient, profile: StationProfile, soc_kwh: float, tick_seconds: float, stop_event: threading.Event) -> None:
    boot_id = f"mock-{int(time.time())}"
    sequence = 0
    last_heartbeat = 0.0

    while not stop_event.is_set():
        now = datetime.now(UTC)

        if time.time() - last_heartbeat > 60:
            try:
                client.heartbeat(boot_id, "1.0.0-mock-device", {"max_charge_w": profile.battery_max_charge_kw * 1000})
                last_heartbeat = time.time()
            except Exception as exc:
                logger.warning("heartbeat esuat: %s", exc)

        override_kw = _handle_plan_and_commands(client, profile.label)

        sample = simulate_tick(profile, now, soc_kwh, dt_hours=tick_seconds / 3600, override_battery_power_kw=override_kw)
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
            "raw_payload": {"simulated": True, "scenario": "mock_device"},
        }
        try:
            client.send_telemetry([item])
            logger.info(
                "tick #%d: PV=%.0fW consum=%.0fW baterie=%.0fW retea=%.0fW SOC=%.1f%%",
                sequence, sample.pv_power_w, sample.load_power_w, sample.battery_power_w,
                sample.grid_power_w, sample.battery_soc_percent,
            )
        except Exception as exc:
            logger.warning("trimitere telemetrie esuata: %s", exc)

        stop_event.wait(tick_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m simulator.run_mock_device",
        description=(
            "Ruleaza un singur dispozitiv simulat (telemetrie plauzibila in timp real, "
            "heartbeat, acceptare automata de planuri/comenzi) impotriva unei statii "
            "existente, fara fisier de seed. Vezi docstring-ul modulului pentru pasii "
            "compleți de configurare."
        ),
    )
    parser.add_argument(
        "--claim-code",
        help="Codul de asociere generat din UI pentru statia tinta (necesar doar la prima rulare).",
    )
    parser.add_argument(
        "--label", default="mock-device",
        help="Nume/cheie locala pentru acest dispozitiv (folosit pentru fisierul de stare si etichetarea log-urilor).",
    )
    parser.add_argument("--api-base-url", default=os.environ.get("SIMULATOR_API_BASE_URL", "http://localhost:8000/api/v1"))
    parser.add_argument("--state-dir", default=os.environ.get("SIMULATOR_STATE_DIR", "./.simulator_state"))
    parser.add_argument("--tick-seconds", type=float, default=_env_float("SIMULATOR_TICK_SECONDS", 20.0))

    parser.add_argument("--pv-kwp", type=float, default=5.0, help="Putere PV instalata (kWp).")
    parser.add_argument("--inverter-kw", type=float, default=5.0, help="Putere invertor (kW).")
    parser.add_argument("--battery-kwh", type=float, default=10.0, help="Capacitate baterie (kWh).")
    parser.add_argument(
        "--battery-max-charge-kw", type=float, default=None,
        help="Putere maxima incarcare baterie (kW); implicit jumatate din capacitate.",
    )
    parser.add_argument(
        "--battery-max-discharge-kw", type=float, default=None,
        help="Putere maxima descarcare baterie (kW); implicit jumatate din capacitate.",
    )
    parser.add_argument("--min-soc", type=float, default=15.0, help="SOC minim de rezerva (%%).")
    parser.add_argument("--max-soc", type=float, default=95.0, help="SOC maxim normal (%%).")
    parser.add_argument("--allow-grid-charge", action="store_true")
    parser.add_argument("--allow-battery-export", action="store_true")
    parser.add_argument("--ev-enabled", action="store_true")
    parser.add_argument("--ev-battery-kwh", type=float, default=0.0)
    parser.add_argument("--ev-max-charge-kw", type=float, default=0.0)
    parser.add_argument("--load-base-kw", type=float, default=0.5, help="Consum de baza (kW).")
    parser.add_argument("--load-peak-kw", type=float, default=2.2, help="Consum de varf, seara (kW).")
    parser.add_argument("--timezone", default="Europe/Bucharest")
    parser.add_argument("--seed", type=int, default=42, help="Seed pentru reproductibilitatea variatiei (nori/consum).")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    profile = build_profile(args)

    client = DeviceApiClient(args.api_base_url)
    state = SimulatorState(args.state_dir)

    try:
        ensure_claimed(client, state, args.label, args.claim_code)

        soc_kwh = profile.battery_capacity_kwh * 0.5
        stop_event = threading.Event()

        def _handle_signal(signum, frame):
            logger.info("Oprire...")
            stop_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        logger.info(
            "Pornesc dispozitivul mock '%s': PV=%.1fkWp invertor=%.1fkW baterie=%.1fkWh -> %s",
            args.label, profile.pv_kwp, profile.inverter_kw, profile.battery_capacity_kwh, args.api_base_url,
        )
        live_loop(client, profile, soc_kwh, args.tick_seconds, stop_event)
    finally:
        client.close()


if __name__ == "__main__":
    main()
