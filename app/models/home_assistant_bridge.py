"""Station-scoped outbound Home Assistant bridge and ephemeral context."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class HomeAssistantBridge(Entity):
    __tablename__ = "home_assistant_bridges"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), unique=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    consent_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    pairing_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instance_id: Mapped[uuid.UUID | None] = mapped_column()
    instance_name: Mapped[str | None] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mapping_version: Mapped[int] = mapped_column(Integer, default=0)
    mappings: Mapped[list] = mapped_column(JSON, default=list)
    observations: Mapped[dict] = mapped_column(JSON, default=dict)
    occupancy_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    insights_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
