from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class HomeAssistantConnection(Entity):
    __tablename__ = "home_assistant_connections"
    __table_args__ = (CheckConstraint("revision > 0", name="ck_ha_revision"),)

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), unique=True
    )
    broker_key: Mapped[str] = mapped_column(String(80))
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    stream_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(32))
    rejected_messages: Mapped[int] = mapped_column(Integer, default=0)


class HomeAssistantMapping(Entity):
    __tablename__ = "home_assistant_mappings"
    __table_args__ = (
        UniqueConstraint("connection_id", "entity_id", name="uq_ha_mapping_entity"),
        CheckConstraint("max_age_seconds BETWEEN 30 AND 3600", name="ck_ha_mapping_age"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("home_assistant_connections.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(12))
    max_age_seconds: Mapped[int] = mapped_column(Integer, default=180)
    # Only the latest minimised observation is retained, never HA attributes/history.
    value: Mapped[object | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sample_id: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str | None] = mapped_column(String(24))
    quality: Mapped[str] = mapped_column(String(16), default="unknown")
    available: Mapped[bool] = mapped_column(Boolean, default=False)
