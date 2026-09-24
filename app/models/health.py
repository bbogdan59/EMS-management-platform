from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class HealthState(Entity):
    __tablename__ = "health_states"
    __table_args__ = (UniqueConstraint("station_id", "rule", "subject", name="uq_health_state"),)

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), index=True
    )
    rule: Mapped[str] = mapped_column(String(48))
    subject: Mapped[str] = mapped_column(String(64), default="station")
    alert_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    healthy_windows: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HealthEvaluation(Entity):
    __tablename__ = "health_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "station_id", "rule", "subject", "version", "window_end", name="uq_health_evaluation"
        ),
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), index=True
    )
    rule: Mapped[str] = mapped_column(String(48))
    subject: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    verdict: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[dict] = mapped_column(JSON)


class AlertEvent(Entity):
    __tablename__ = "alert_events"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(500))


class DiagnosticGrant(Entity):
    __tablename__ = "diagnostic_grants"
    __table_args__ = (UniqueConstraint("station_id", "user_id", name="uq_diagnostic_grant"),)

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
