from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity

# Conventii de semn (documentate si in docs/API.md):
#   battery_power_w: pozitiv = incarcare, negativ = descarcare
#   grid_power_w:    pozitiv = import din retea, negativ = export in retea
#   pv_power_w, load_power_w, ev_power_w: intotdeauna >= 0


class TelemetryRaw(Entity):
    """Un mesaj de telemetrie primit de la un dispozitiv.

    Deduplicare: (device_id, boot_id, sequence) trebuie sa fie unic -- un
    dispozitiv care repeta o secventa dupa un restart foloseste un boot_id nou.
    """

    __tablename__ = "telemetry_raw"
    __table_args__ = (
        UniqueConstraint("device_id", "boot_id", "sequence", name="uq_telemetry_dedup"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pv_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    load_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    battery_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    grid_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    battery_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    ev_connected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ev_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    quality_flags: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TelemetryAggregate(Entity):
    """Agregate energetice pe interval (15m/ora/zi/luna), folosite pentru
    grafice si retentie pe termen lung dupa expirarea datelor brute."""

    __tablename__ = "telemetry_aggregates"
    __table_args__ = (
        UniqueConstraint("station_id", "period_type", "period_start", name="uq_telemetry_aggregate_period"),
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)  # interval_15m|hour|day|month
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pv_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    load_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    battery_charge_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    battery_discharge_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    grid_import_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    grid_export_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    ev_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)

    avg_battery_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_quality: Mapped[str] = mapped_column(String(16), default="measured", nullable=False)
