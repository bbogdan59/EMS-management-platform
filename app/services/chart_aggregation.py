"""Agregare server-side, metric-aware, pentru charturile din dashboard (issue #33).

Contract de rezolutie (politica server-side, nu resamplare arbitrara in
browser):
  - intervale foarte detaliate (<=24h): 15 minute;
  - intervale scurte/intermediare (cateva zile): 30 minute;
  - 30 zile: 1 ora;
  - 1 an: 1 zi.

Agregarea este metric-aware: puterea (kW) se agrega prin MEDIE (nu se
insumeaza kW-uri instantanee dintr-un bucket), iar procentele/SOC-ul se
agrega STRICT prin medie -- niciodata prin suma. O metrica de energie
(kWh, aditiva in timp) poate folosi suma. `aggregate_series` refuza explicit
(ValueError) o cerere de "sum" pe o coloana ce arata a procent/SOC, ca sa nu
se poata reintroduce din greseala bug-ul "SOC insumat" intr-un apel viitor.

Lipsa de date NU devine niciodata 0: un bucket fara nicio valoare non-null
pentru o metrica ramane `None` pentru acea metrica.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from statistics import mean
from typing import Literal

AggregationMethod = Literal["mean", "sum", "min", "max"]

# range_key (asa cum e trimis de UI, in query string) -> rezolutie tinta.
_RANGE_RESOLUTION: dict[str, str] = {
    "24h": "15m",
    "7d": "30m",
    "30d": "1h",
    "1y": "1d",
}

RESOLUTION_SECONDS: dict[str, int] = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
}

_PERCENT_LIKE_SUBSTRINGS = ("soc", "pct", "percent", "_soc")


def choose_resolution(range_key: str) -> str:
    """Alege rezolutia server-side pentru un `range_key` de UI (24h/7d/30d/1y).

    Necunoscut -> cea mai detaliata rezolutie (15m), niciodata rezolutie bruta
    nelimitata -- un range_key nerecunoscut nu trebuie sa devina o portita spre
    un raspuns nemarginit.
    """
    return _RANGE_RESOLUTION.get(range_key, "15m")


def _looks_like_percentage(metric_name: str) -> bool:
    lowered = metric_name.lower()
    return any(token in lowered for token in _PERCENT_LIKE_SUBSTRINGS)


def bucket_start(ts: datetime, bucket_seconds: int) -> datetime:
    epoch_seconds = ts.timestamp()
    floored = (epoch_seconds // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floored, tz=ts.tzinfo)


def aggregate_series(
    rows: Sequence[dict],
    *,
    timestamp_key: str,
    bucket_seconds: int,
    metrics: dict[str, AggregationMethod],
) -> list[dict]:
    """Grupeaza `rows` (dict-uri cu cel putin `timestamp_key`) in bucket-uri de
    `bucket_seconds`, aplicand metoda de agregare declarata per coloana in
    `metrics`.

    Randurile de intrare trebuie sa fie deja sortate crescator dupa
    `timestamp_key` (asa cum vin din interogarea SQL `ORDER BY measured_at`) --
    functia pastreaza ordinea bucket-urilor de intalnire, nu re-sorteaza.
    """
    for name, method in metrics.items():
        if method == "sum" and _looks_like_percentage(name):
            raise ValueError(
                f"refuz sa insumez metrica de tip procent/SOC '{name}' -- "
                "foloseste 'mean' (sau 'min'/'max')."
            )

    buckets: dict[datetime, dict[str, list[float]]] = {}
    order: list[datetime] = []
    for row in rows:
        ts = row[timestamp_key]
        bucket_ts = bucket_start(ts, bucket_seconds)
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {name: [] for name in metrics}
            order.append(bucket_ts)
        for name in metrics:
            value = row.get(name)
            if value is not None:
                buckets[bucket_ts][name].append(float(value))

    result: list[dict] = []
    for bucket_ts in order:
        values = buckets[bucket_ts]
        point: dict = {timestamp_key: bucket_ts}
        for name, method in metrics.items():
            samples = values[name]
            if not samples:
                point[name] = None
                continue
            if method == "mean":
                point[name] = mean(samples)
            elif method == "sum":
                point[name] = sum(samples)
            elif method == "min":
                point[name] = min(samples)
            elif method == "max":
                point[name] = max(samples)
            else:  # pragma: no cover - garantat de tipul AggregationMethod
                raise ValueError(f"metoda de agregare necunoscuta: {method}")
        result.append(point)
    return result


def compute_coverage(
    rows: Sequence[dict], *, timestamp_key: str, start: datetime, end: datetime, bucket_seconds: int
) -> float:
    """Fractia de bucket-uri asteptate in [start, end) care contin cel putin
    un punct brut. Nu masoara "cate puncte brute lipsesc dintr-un bucket
    dens" -- doar daca bucketul e complet gol -- suficient pentru a semnala
    onest o fereastra cu gauri mari, fara sa pretinda o precizie pe care
    calculul nu o are."""
    expected_buckets = max(1, math.ceil((end - start).total_seconds() / bucket_seconds))
    present: set[datetime] = set()
    for row in rows:
        present.add(bucket_start(row[timestamp_key], bucket_seconds))
    return min(1.0, len(present) / expected_buckets)
