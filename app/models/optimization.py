from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity

# Lantul de stari cerut explicit de specificatie:
#   OptimizationRun (scenariul calculat)
#     -> Plan (planul publicat, daca run-ul a reusit)
#         -> Plan.accepted_at / accepted_by_device_id (acceptarea de catre dispozitiv)
#         -> Command-uri emise per interval, cu CommandEvent-uri de confirmare (executia confirmata)
#         -> PlanInterval.observed_* (efectul observat, calculat ulterior din telemetrie/agregate)


class OptimizationRun(Entity):
    __tablename__ = "optimization_runs"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # OptimizationRunStatus
    horizon_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)

    station_config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("station_config_versions.id"), nullable=True
    )
    preference_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("preference_versions.id"), nullable=True
    )
    input_snapshot: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False, doc="Snapshot al intrarilor (tarife, preturi, prognoze) pt. reproductibilitate."
    )

    solver_name: Mapped[str] = mapped_column(String(32), default="appsi_highs", nullable=False)
    solver_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    objective_value_lei: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    solver_timeout_seconds: Mapped[float] = mapped_column(Numeric(6, 2), default=30, nullable=False)

    triggered_by: Mapped[str] = mapped_column(String(24), default="scheduler", nullable=False)  # scheduler|user|event
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    explanation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    plan: Mapped[Plan | None] = relationship(back_populates="optimization_run", uselist=False)


class Plan(Entity):
    __tablename__ = "plans"

    optimization_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("optimization_runs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # PlanStatus
    execution_mode: Mapped[str] = mapped_column(String(16), default="shadow", nullable=False)  # ExecutionMode

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"), nullable=True)

    __table_args__ = (UniqueConstraint("station_id", "version", name="uq_plan_station_version"),)

    optimization_run: Mapped[OptimizationRun] = relationship(back_populates="plan")
    intervals: Mapped[list[PlanInterval]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanInterval.interval_start"
    )


class PlanInterval(Entity):
    __tablename__ = "plan_intervals"
    __table_args__ = (UniqueConstraint("plan_id", "interval_start", name="uq_plan_interval_start"),)

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pv_forecast_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    load_forecast_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    battery_power_target_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)  # + charge, - discharge
    grid_power_target_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)  # + import, - export
    battery_soc_target_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    ev_charge_power_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0, nullable=False)

    price_import_lei_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    price_export_lei_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)

    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    observed_battery_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    observed_grid_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    observed_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    deviation_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    plan: Mapped[Plan] = relationship(back_populates="intervals")
