"""Backfill istoric al preturilor OPCOM PZU pentru un interval de date.

Populeaza `market_price_intervals`/`import_runs` pentru fiecare zi din
interval, reutilizand exact acelasi adaptor (`app.services.opcom_service`)
folosit de importul zilnic automat -- aceeasi validare, aceeasi idempotenta
(revizii), acelasi fallback sintetic marcat explicit daca sursa reala nu e
accesibila pentru o zi anume.

Eficienta:
  - o singura interogare initiala determina zilele deja importate cu succes
    (ne-sintetic), care sunt sarite fara nicio cerere HTTP;
  - zilele ramase sunt preluate CONCURENT (ThreadPoolExecutor), fiecare fir
    de executie cu propria sesiune SQLAlchemy (sesiunile nu sunt partajate
    intre thread-uri) -- implicit 8 cereri simultane, configurabil;
  - reincercarile per-zi (backoff exponential) sunt deja gestionate de
    `opcom_service._fetch_raw`; aici se adauga doar paralelism la nivel de zi.

Utilizare:
    python -m scripts.backfill_opcom_history --start 2024-01-01
    python -m scripts.backfill_opcom_history --start 2024-01-01 --end 2024-12-31 --concurrency 4
    python -m scripts.backfill_opcom_history --start 2024-01-01 --force   # reimporta si zilele deja reusite

Fara --end, se opreste la ziua curenta (UTC). Scriptul e SIGUR de reluat --
poate fi intrerupt si rerulat oricand, reia doar de unde a ramas.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models.enums import ImportRunStatus  # noqa: E402
from app.services import opcom_service  # noqa: E402

# Motor SQLAlchemy DEDICAT acestui script, dimensionat dupa --concurrency, in
# loc sa refolosim engine-ul global al aplicatiei (dimensionat pentru
# incarcarea web normala, pool_size=10 -- prea mic pentru zeci de conexiuni
# concurente de backfill si ar epuiza pool-ul cu 'QueuePool limit reached').
_engine = None
_SessionFactory = None


def _init_engine(concurrency: int) -> None:
    global _engine, _SessionFactory
    settings = get_settings()
    _engine = create_engine(settings.database_url, pool_size=concurrency, max_overflow=concurrency, pool_pre_ping=True)
    _SessionFactory = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _import_one_day(d: date) -> tuple[date, str, bool, str | None]:
    """Ruleaza intr-un thread separat, cu propria sesiune DB. Returneaza
    (data, status, is_synthetic, eroare)."""
    db = _SessionFactory()
    try:
        run = opcom_service.import_opcom_day(db, d)
        db.commit()
        return d, run.status, run.is_synthetic_fixture, run.error_message
    except Exception as exc:  # nu lasa o zi problematica sa opreasca tot backfill-ul
        db.rollback()
        return d, "exception", False, str(exc)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="Data de inceput (YYYY-MM-DD), ex. 2024-01-01")
    parser.add_argument("--end", default=None, help="Data de sfarsit (YYYY-MM-DD). Implicit: azi (UTC).")
    parser.add_argument("--concurrency", type=int, default=8, help="Nr. de cereri concurente (implicit 8).")
    parser.add_argument("--force", action="store_true", help="Reimporta si zilele deja reusite (creeaza o revizie noua).")
    args = parser.parse_args()

    _init_engine(args.concurrency)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else datetime.utcnow().date()
    if start > end:
        print(f"Data de inceput ({start}) e dupa data de sfarsit ({end}).", file=sys.stderr)
        sys.exit(1)

    all_days = list(_daterange(start, end))
    print(f"Interval: {start} -> {end} ({len(all_days)} zile).")

    if args.force:
        pending_days = all_days
        print("--force: reimport pentru toate zilele din interval.")
    else:
        with _SessionFactory() as db:
            already_done = opcom_service.get_real_imported_dates(db, start, end)
        pending_days = [d for d in all_days if d not in already_done]
        print(f"Deja importate cu succes (date reale, ne-sintetice): {len(already_done)}. Ramase de importat: {len(pending_days)}.")

    if not pending_days:
        print("Nimic de facut -- tot intervalul e deja importat. Foloseste --force pentru reimport.")
        return

    started_at = time.monotonic()
    results = {"succeeded": 0, "synthetic": 0, "unpublished": 0, "failed": 0}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(_import_one_day, d): d for d in pending_days}
        done_count = 0
        for future in as_completed(futures):
            d, status, is_synthetic, error = future.result()
            done_count += 1

            if status == ImportRunStatus.succeeded.value and not is_synthetic:
                results["succeeded"] += 1
            elif status == ImportRunStatus.succeeded.value and is_synthetic:
                results["synthetic"] += 1
            elif status == ImportRunStatus.unpublished.value:
                results["unpublished"] += 1
            else:
                results["failed"] += 1
                errors.append(f"{d}: status={status} eroare={error}")

            if done_count % 25 == 0 or done_count == len(pending_days):
                elapsed = time.monotonic() - started_at
                print(f"  [{done_count}/{len(pending_days)}] ({elapsed:.0f}s) -- ultima zi procesata: {d} ({status}{' SINTETIC' if is_synthetic else ''})")

    elapsed = time.monotonic() - started_at
    print("\n--- Rezumat backfill OPCOM ---")
    print(f"Durata: {elapsed:.1f}s pentru {len(pending_days)} zile ({len(pending_days) / elapsed:.1f} zile/s)" if elapsed > 0 else "")
    print(f"Reusite (date reale):     {results['succeeded']}")
    print(f"Reusite (fallback sintetic, MARCAT ca atare in DB): {results['synthetic']}")
    print(f"Nepublicate (viitor prea indepartat): {results['unpublished']}")
    print(f"Esuate:                   {results['failed']}")
    if errors:
        print("\nZile cu eroare:")
        for e in errors[:50]:
            print(f"  - {e}")
        if len(errors) > 50:
            print(f"  ... si inca {len(errors) - 50}.")

    if results["synthetic"] > 0:
        print(
            "\nATENTIE: unele zile au fost importate cu date SINTETICE (fallback), pentru ca "
            "sursa reala (opcom.ro) nu a fost accesibila din acest mediu. Sunt marcate explicit "
            "in baza de date (import_runs.is_synthetic_fixture=true) si vizibile ca atare in UI/admin. "
            "Ruleaza din nou acest script (fara --force) dintr-un mediu cu acces la opcom.ro pentru a "
            "inlocui acele zile cu date reale -- scriptul reia automat doar zilele ramase de facut real."
        )


if __name__ == "__main__":
    main()
