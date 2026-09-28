"""Astronomical sun positions for a station's local calendar day."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from math import isfinite
from zoneinfo import ZoneInfo

import pandas as pd
from pvlib.solarposition import get_solarposition, sun_rise_set_transit_spa


def _positions(times: pd.DatetimeIndex, latitude: float, longitude: float) -> list[dict]:
    positions = get_solarposition(times, latitude, longitude, method="nrel_numpy")
    return [
        {
            "at": instant.isoformat(),
            "azimuth": round(float(row.azimuth), 4),
            "elevation": round(float(row.apparent_elevation), 4),
            "is_daylight": bool(row.elevation >= -0.8333),
        }
        for instant, row in positions.iterrows()
    ]


@lru_cache(maxsize=256)
def _day_path(latitude: float, longitude: float, timezone: str, local_date: date) -> dict:
    tz = ZoneInfo(timezone)
    start = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    # A local day can contain events from adjacent UTC dates (and 23 or 25 hours).
    candidates = pd.date_range(start.date() - timedelta(days=1), end.date() + timedelta(days=1), tz="UTC")
    events = sun_rise_set_transit_spa(candidates, latitude, longitude)
    selected = {}
    for name in ("sunrise", "sunset", "transit"):
        values = pd.to_datetime(events[name].dropna(), utc=True)
        values = values[(values >= start) & (values < end)]
        selected[name] = values.iloc[0].round("s") if len(values) else None
    event_times = [value for value in selected.values() if value is not None]
    times = pd.date_range(start, end, freq="5min", inclusive="left").union(pd.DatetimeIndex(event_times, tz="UTC")).sort_values()
    path = _positions(times, latitude, longitude)
    by_time = {point["at"]: point for point in path}
    return {
        "local_date": local_date.isoformat(),
        "day_start": start.isoformat(),
        "day_end": end.isoformat(),
        "path": path,
        **{name: by_time[value.isoformat()] if value is not None else None for name, value in selected.items()},
    }


def sun_map(latitude, longitude, timezone: str, now: datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Sun position requires a timezone-aware instant")
    now = now.astimezone(UTC)
    base = {"timezone": timezone, "calculated_at": now.isoformat(), "source": "calculated"}
    if latitude is None or longitude is None:
        return {**base, "status": "missing_location", "location": None, "current": None, "path": []}
    latitude, longitude = float(latitude), float(longitude)
    if not (isfinite(latitude) and isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {**base, "status": "invalid_location", "location": None, "current": None, "path": []}
    local_date = now.astimezone(ZoneInfo(timezone)).date()
    return {
        **base,
        **_day_path(latitude, longitude, timezone, local_date),
        "status": "ok",
        "location": {"latitude": latitude, "longitude": longitude},
        "current": _positions(pd.DatetimeIndex([now]), latitude, longitude)[0],
    }
