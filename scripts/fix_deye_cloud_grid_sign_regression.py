"""Corecteaza `TelemetryRaw.grid_power_w` pentru randurile Deye Cloud scrise
cu semnul INVERSAT in timpul regresiei PR #173 -> PR #178 (2026-09-18 16:17
CEST - 2026-09-23 11:46 CEST): `grid_w = -wire_w` in loc de `grid_w = wire_w`.
Simptom raportat: import/export afisate inversat pe dashboard in cardurile
"Astazi"/"Luna curenta".

`map_station_latest_to_telemetry` (vezi `app.services.deye_cloud_service`)
mapeaza corect ACUM, dar semnul e persistat definitiv la ingestie -- randurile
scrise in fereastra de regresie raman gresite pentru totdeauna pana sunt
corectate explicit aici. Nicio recalculare la citire nu exista in platforma
(contract documentat: energia si banii raman Decimal in domeniu/stocare,
convertite doar la prezentare -- semnul e parte din valoarea stocata, nu un
artefact de afisare).

Strategie: NU presupunem fereastra UTC exacta (risc de eroare de fus orar/
offset) -- recalculam `grid_power_w` din `raw_payload["wirePower"]`, sursa
originala nealterata a raspunsului Deye Cloud (stocata neschimbata la fiecare
ingestie, `poll_connection`/`import_station_history`), folosind EXACT aceeasi
functie de mapare (`_dec` + atribuirea `grid_w = wire_w`) ca ingestia curenta,
corecta. Auto-vindecator si idempotent: un rand deja corect nu se schimba;
rulat de mai multe ori converge la aceeasi valoare, nu inverseaza a doua oara.
Scop STRICT limitat la `grid_power_w` -- `battery_power_w`/`pv_power_w`/
`load_power_w` nu au fost niciodata afectate de aceasta regresie (vezi
`deye_cloud_service.py`, diff #173) si raman neatinse.

Dupa corectare, reagregheaza (idempotent, UPSERT) toate statiile/intervalele
atinse, ca `telemetry_aggregates` (si deci cardurile dashboard) sa reflecte
datele corectate.

Utilizare:
    python -m scripts.fix_deye_cloud_grid_sign_regression --dry-run
    python -m scripts.fix_deye_cloud_grid_sign_regression

Sigur de rulat repetat -- a doua rulare nu gaseste randuri de corectat.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models.enums import TelemetrySource
from app.models.station import Station
from app.models.telemetry import TelemetryRaw
from app.services import aggregation_service
from app.services.deye_cloud_service import _dec

_BATCH_SIZE = 500


def correct_deye_cloud_grid_sign(db: Session, *, dry_run: bool = False) -> dict:
    """Corecteaza in `db` toate randurile `TelemetryRaw` Deye Cloud a caror
    `grid_power_w` nu (mai) corespunde cu `raw_payload["wirePower"]` mapat
    prin logica ACTUALA (corecta) -- vezi docstring-ul modulului. Reagreheaza
    statiile/intervalele atinse daca `dry_run` e False. Functie pura de
    logica, reutilizabila din `main()` (CLI) si din teste."""
    corrected = 0
    unchanged = 0
    stations_touched: dict = {}  # station_id -> [min_measured_at, max_measured_at]

    last_id = None
    while True:
        query = (
            select(TelemetryRaw)
            .where(
                TelemetryRaw.source == TelemetrySource.deye_cloud.value,
                TelemetryRaw.grid_power_w.isnot(None),
            )
            .order_by(TelemetryRaw.id)
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            query = query.where(TelemetryRaw.id > last_id)
        rows = db.scalars(query).all()
        if not rows:
            break

        for row in rows:
            last_id = row.id
            correct_value = _dec((row.raw_payload or {}).get("wirePower"))
            if correct_value is None or correct_value == row.grid_power_w:
                unchanged += 1
                continue
            corrected += 1
            bounds = stations_touched.setdefault(row.station_id, [row.measured_at, row.measured_at])
            bounds[0] = min(bounds[0], row.measured_at)
            bounds[1] = max(bounds[1], row.measured_at)
            if not dry_run:
                row.grid_power_w = correct_value
                db.add(row)

    if dry_run:
        return {"corrected": corrected, "unchanged": unchanged, "stations_touched": stations_touched}

    db.flush()
    for station_id, (start, end) in stations_touched.items():
        station = db.get(Station, station_id)
        if station is None:
            continue
        aggregation_service.reaggregate_range(db, station, start, end + timedelta(minutes=15))

    return {"corrected": corrected, "unchanged": unchanged, "stations_touched": stations_touched}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Doar raporteaza cate randuri ar fi corectate, fara sa scrie.")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with SessionFactory() as db:
        result = correct_deye_cloud_grid_sign(db, dry_run=args.dry_run)
        print(f"Randuri Deye Cloud corectate: {result['corrected']}. Ramase corecte/nemodificate: {result['unchanged']}.")

        if args.dry_run:
            if result["stations_touched"]:
                print("--dry-run: nicio scriere efectuata. Statii care ar fi atinse:")
                for station_id, (start, end) in result["stations_touched"].items():
                    print(f"  - {station_id}: {start.isoformat()} .. {end.isoformat()}")
            else:
                print("--dry-run: nimic de corectat.")
            return

        db.commit()
        if result["stations_touched"]:
            print(f"Reagregare finalizata pentru {len(result['stations_touched'])} statie(i).")
        else:
            print("Nimic de reagregat -- nicio corectie aplicata.")


if __name__ == "__main__":
    main()
