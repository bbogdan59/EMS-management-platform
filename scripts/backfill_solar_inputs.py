"""Idempotent replay of retained device diagnostics, never station energy."""

import argparse
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.core.security import utcnow
from app.database import session_scope
from app.models.device import Device
from app.models.station import Station
from app.models.telemetry import TelemetryRaw
from app.services import solar_service


def replay_day(db, station, start, end):
    query = (
        select(TelemetryRaw, Device.name)
        .join(Device, Device.id == TelemetryRaw.device_id)
        .where(
            TelemetryRaw.station_id == station.id,
            TelemetryRaw.measured_at >= start - solar_service.MAX_GAP,
            TelemetryRaw.measured_at < end,
        )
        .order_by(TelemetryRaw.device_id, TelemetryRaw.measured_at, TelemetryRaw.received_at)
    )
    count = 0
    for row, name in db.execute(query).yield_per(500):
        if not (row.diagnostics or {}).get("mppt"):
            continue
        # Keep the historical station association even if the device was moved.
        device = SimpleNamespace(id=row.device_id, station_id=row.station_id, name=name)
        solar_service.ingest_device(
            db,
            device,
            {
                field: getattr(row, field)
                for field in (
                    "diagnostics",
                    "quality_flags",
                    "is_simulated",
                    "is_late",
                    "measured_at",
                    "received_at",
                )
            },
        )
        count += 1
    solar_service.reaggregate(db, station, start, end - timedelta(microseconds=1))
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", required=True, type=UUID)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.days <= get_settings().telemetry_raw_retention_days:
        parser.error("days must be between 1 and the configured raw retention")
    end = utcnow()
    start = end - timedelta(days=args.days)
    count = 0
    while start < end:
        finish = min(end, start + timedelta(days=1))
        with session_scope() as db:
            station = db.get(Station, args.station)
            if station is None:
                parser.error("Station not found")
            count += replay_day(db, station, start, finish)
        start = finish
    print(f"Replayed {count} retained diagnostic records (idempotent).")


if __name__ == "__main__":
    main()
