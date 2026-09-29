"""Provider voltage observations, separate from energy integration samples."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class GridVoltageSample(Entity):
    __tablename__ = "grid_voltage_samples"
    __table_args__ = (
        UniqueConstraint("station_id", "source_id", "measured_at", name="uq_grid_voltage_time"),
        Index("ix_grid_voltage_station_source_time", "station_id", "source_id", "measured_at"),
    )
    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"))
    # Inventory removal/reconnection must not delete previously collected history.
    source_id: Mapped[uuid.UUID] = mapped_column()
    label: Mapped[str] = mapped_column(String(200))
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    l1_v: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    l2_v: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    l3_v: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    reported_phases: Mapped[list] = mapped_column(JSON)
