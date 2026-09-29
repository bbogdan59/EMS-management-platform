"""Per-inverter input diagnostics, isolated from station energy accounting."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class SolarInverter(Entity):
    __tablename__ = "solar_inverters"
    __table_args__ = (
        UniqueConstraint(
            "station_id", "device_id", "source", "source_key", name="uq_solar_inverter_source"
        ),
    )
    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))
    source_key: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(200))
    model_name: Mapped[str | None] = mapped_column(String(120))
    ac_observation: Mapped[dict] = mapped_column(JSON, default=dict)


class SolarTracker(Entity):
    __tablename__ = "solar_trackers"
    __table_args__ = (
        UniqueConstraint("inverter_id", "input_index", name="uq_solar_tracker_index"),
    )
    inverter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solar_inverters.id", ondelete="CASCADE"), index=True
    )
    input_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="mppt")
    label: Mapped[str] = mapped_column(String(80))
    supported_metrics: Mapped[list] = mapped_column(JSON, default=list)
    last_reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class SolarInputSample(Entity):
    __tablename__ = "solar_input_samples"
    __table_args__ = (UniqueConstraint("tracker_id", "measured_at", name="uq_solar_sample_time"),)
    tracker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solar_trackers.id", ondelete="CASCADE"), index=True
    )
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    voltage_v: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    current_a: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    power_w: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    quality: Mapped[str] = mapped_column(String(16))
    flags: Mapped[list] = mapped_column(JSON, default=list)


class SolarInputAggregate(Entity):
    __tablename__ = "solar_input_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "tracker_id", "period_type", "period_start", name="uq_solar_aggregate_period"
        ),
    )
    tracker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solar_trackers.id", ondelete="CASCADE"), index=True
    )
    period_type: Mapped[str] = mapped_column(String(16))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    voltage_v: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    current_a: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    power_w: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    coverage: Mapped[dict] = mapped_column(JSON)
    flags: Mapped[list] = mapped_column(JSON)
    quality: Mapped[str] = mapped_column(String(16))
