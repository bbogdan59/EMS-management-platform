from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity


class Alert(Entity):
    __tablename__ = "alerts"

    station_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=True, index=True,
        doc="Nul pentru alerte la nivel de platforma (ex. import OPCOM esuat, fara statie asociata).",
    )
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    # ex: device_offline, telemetry_stale, opcom_unpublished, optimization_failed,
    #     command_rejected, solver_timeout, config_conflict
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # AlertSeverity
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # AlertStatus
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    station: Mapped[Station] = relationship()  # noqa: F821
