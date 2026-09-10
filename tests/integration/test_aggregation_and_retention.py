from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.security import utcnow
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.services import aggregation_service
from tests.factories import make_device, make_org, make_station, make_user


def test_aggregate_interval_15m_skips_when_no_samples(db):
    user = make_user(db, email="agg1@test.local")
    org = make_org(db, "Agg Org 1")
    station = make_station(db, org, user, name="Agg Station 1")
    db.commit()

    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    result = aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    assert result is None  # fara date -> fara rand, nu zero fabricat

    existing = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert existing is None


def test_aggregate_interval_15m_computes_energy(db):
    user = make_user(db, email="agg2@test.local")
    org = make_org(db, "Agg Org 2")
    station = make_station(db, org, user, name="Agg Station 2")
    db.commit()

    device = make_device(db, station)
    period_start = utcnow().replace(minute=0, second=0, microsecond=0)
    for minute_offset, pv_w in [(0, 2000), (5, 2000)]:
        db.add(
            TelemetryRaw(
                device_id=device.id, station_id=station.id, boot_id="b", sequence=minute_offset,
                measured_at=period_start + timedelta(minutes=minute_offset), received_at=utcnow(),
                pv_power_w=Decimal(pv_w), load_power_w=Decimal(500), battery_power_w=Decimal(0), grid_power_w=Decimal(-1500),
                battery_soc_percent=Decimal("50"),
            )
        )
    db.commit()

    aggregation_service.aggregate_interval_15m(db, station.id, period_start)
    db.commit()

    row = db.scalar(
        select(TelemetryAggregate).where(
            TelemetryAggregate.station_id == station.id, TelemetryAggregate.period_start == period_start
        )
    )
    assert row is not None
    assert row.sample_count == 2
    # 2000 W * 0.25h = 0.5 kWh
    assert abs(float(row.pv_energy_kwh) - 0.5) < 1e-6
    assert row.data_quality in ("measured", "estimated")


def test_retention_deletes_old_telemetry(db):
    user = make_user(db, email="ret1@test.local")
    org = make_org(db, "Ret Org 1")
    station = make_station(db, org, user, name="Ret Station 1")
    db.commit()

    device = make_device(db, station)
    old_time = utcnow() - timedelta(days=400)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="b", sequence=1,
            measured_at=old_time, received_at=old_time, pv_power_w=Decimal(100),
        )
    )
    recent_time = utcnow() - timedelta(hours=1)
    db.add(
        TelemetryRaw(
            device_id=device.id, station_id=station.id, boot_id="b", sequence=2,
            measured_at=recent_time, received_at=recent_time, pv_power_w=Decimal(200),
        )
    )
    db.commit()

    from app.config import get_settings

    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.telemetry_raw_retention_days)
    deleted = db.query(TelemetryRaw).filter(TelemetryRaw.measured_at < cutoff).delete(synchronize_session=False)
    db.commit()

    assert deleted == 1
    remaining = db.scalars(select(TelemetryRaw).where(TelemetryRaw.station_id == station.id)).all()
    assert len(remaining) == 1
    assert remaining[0].sequence == 2
