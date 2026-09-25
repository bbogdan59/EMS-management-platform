from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class ControlPolicy(Entity):
    __tablename__ = "control_policies"
    __table_args__ = (UniqueConstraint("station_id", "version", name="uq_control_policy_version"),)

    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    limits: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class StationControl(Entity):
    __tablename__ = "station_controls"

    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), unique=True)
    mode: Mapped[str] = mapped_column(String(16), default="shadow")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("control_policies.id"))
    reason: Mapped[str] = mapped_column(String(500), default="initial_shadow")
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class PlanApproval(Entity):
    __tablename__ = "plan_approvals"

    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), unique=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("control_policies.id"))
    control_revision: Mapped[int] = mapped_column(Integer)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommandVerification(Entity):
    __tablename__ = "command_verifications"

    command_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commands.id", ondelete="CASCADE"), unique=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="awaiting_application")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(String(64))


class PlanOutcome(Entity):
    __tablename__ = "plan_outcomes"

    interval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plan_intervals.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(24))
    evidence: Mapped[dict] = mapped_column(JSON)


class Recommendation(Entity):
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("station_id", "fingerprint", name="uq_recommendation_fingerprint"),)

    station_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="available")
    snapshot: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback: Mapped[str | None] = mapped_column(String(500))
