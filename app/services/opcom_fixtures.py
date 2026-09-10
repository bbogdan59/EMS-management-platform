"""Generator de date sintetice pentru importul OPCOM PZU, folosit DOAR cand
sursa reala (opcom.ro) nu poate fi accesata (retea indisponibila in acest
mediu de dezvoltare/CI, sau eroare persistenta la fetch). Vezi limitarea
documentata in docs/LIMITATIONS.md: schema reala a CSV-ului OPCOM NU a putut
fi verificata direct (acces retea blocat) -- adaptorul foloseste o schema
implicita, configurabila, si valideaza explicit ce gaseste in CSV in loc sa
presupuna orbeste ca se potriveste.

Datele generate aici sunt 100% sintetice si marcate ca atare in baza de date
(ImportRun.is_synthetic_fixture=True) si in UI.
"""
from __future__ import annotations

import csv
import io
import math
import random
from datetime import date

from app.services.opcom_schema import DEFAULT_SCHEMA


def intervals_for_date(d: date) -> int:
    """Numarul de intervale de 15 minute dintr-o zi PZU, tinand cont de
    schimbarea orei (23h -> 92 intervale primavara, 25h -> 100 intervale toamna)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Bucharest")
    start_local = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    # Calculam durata reala in UTC intre cele doua miezuri de noapte locale.
    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))
    hours = (end_utc - start_utc).total_seconds() / 3600
    return round(hours * 4)


def generate_synthetic_csv(d: date) -> str:
    n = intervals_for_date(d)
    rng = random.Random(f"opcom-synthetic-{d.isoformat()}")

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=DEFAULT_SCHEMA.delimiter)
    writer.writerow([DEFAULT_SCHEMA.interval_column, DEFAULT_SCHEMA.price_column, DEFAULT_SCHEMA.currency_column])

    negative_index = rng.randint(int(n * 0.4), int(n * 0.55))
    for i in range(1, n + 1):
        hour_fraction = (i - 1) / (n / 24)
        # Curba orara tipica: cerere/pret scazut noaptea, varf seara.
        base = 350 + 220 * math.sin((hour_fraction - 7) / 24 * 2 * math.pi) + 150 * math.exp(-((hour_fraction - 19) ** 2) / 4)
        noise = rng.uniform(-15, 15)
        price = round(base + noise, 2)
        if i == negative_index:
            price = round(-abs(rng.uniform(5, 40)), 2)
        price_str = f"{price:.2f}".replace(".", ",")
        writer.writerow([str(i), price_str, "RON"])

    return buffer.getvalue()
