from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class EVSE(Entity):
    __tablename__ = "evses"

    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    source: Mapped[str] = mapped_column(String(32), default="manual")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    max_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_days: Mapped[int] = mapped_column(Integer, default=365)
    vehicle_data_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    vacation_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class EVConnector(Entity):
    __tablename__ = "ev_connectors"
    __table_args__ = (UniqueConstraint("evse_id", "number", name="uq_ev_connector_number"),)

    evse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evses.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="unknown")
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Vehicle(Entity):
    __tablename__ = "vehicles"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(100))
    battery_capacity_kwh: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    consumption_kwh_100km: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))


class ChargingSession(Entity):
    __tablename__ = "charging_sessions"
    __table_args__ = (
        Index("uq_ev_active_session", "connector_id", unique=True, postgresql_where=text("ended_at IS NULL")),
    )

    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ev_connectors.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20))
    meter_start_kwh: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    meter_end_kwh: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    source: Mapped[str] = mapped_column(String(32))
    quality: Mapped[str] = mapped_column(String(20), default="unknown")
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)


class EVObservation(Entity):
    __tablename__ = "ev_observations"
    __table_args__ = (UniqueConstraint("connector_id", "event_id", name="uq_ev_observation_event"),)

    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ev_connectors.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("charging_sessions.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(20))
    meter_kwh: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    vehicle_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    meter_epoch: Mapped[str | None] = mapped_column(String(64))
    quality: Mapped[str] = mapped_column(String(20))
    applied: Mapped[bool] = mapped_column(Boolean, default=True)
    payload_hash: Mapped[str] = mapped_column(String(64))


class EVRequirement(Entity):
    __tablename__ = "ev_requirements"

    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ev_connectors.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    minimum_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(9, 3))
    target_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    target_range_km: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active")
    max_cost_lei: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class ChargingPlan(Entity):
    __tablename__ = "charging_plans"
    __table_args__ = (UniqueConstraint("requirement_id", "version", name="uq_charging_plan_version"),)

    requirement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ev_requirements.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    snapshot: Mapped[dict] = mapped_column(JSON)
