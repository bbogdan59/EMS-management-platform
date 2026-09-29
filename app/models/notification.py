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


class NotificationPreference(Entity):
    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_notification_preference"),
        CheckConstraint("briefing_start_hour >= 0 AND briefing_start_hour < briefing_end_hour AND briefing_end_hour <= 24 AND briefing_clock IN ('station', 'user')", name="ck_briefing_window"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Bucharest")
    quiet_start: Mapped[int] = mapped_column(Integer, default=22)
    quiet_end: Mapped[int] = mapped_column(Integer, default=8)
    matrix: Mapped[dict] = mapped_column(JSON, default=dict)
    weekly_report: Mapped[bool] = mapped_column(Boolean, default=False)
    morning_briefing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    briefing_start_hour: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    briefing_end_hour: Mapped[int] = mapped_column(Integer, default=11, server_default="11")
    briefing_clock: Mapped[str] = mapped_column(String(16), default="station", server_default="station")
    escalation_minutes: Mapped[int] = mapped_column(Integer, default=15)
    verified_email: Mapped[str | None] = mapped_column(String(320))
    verification_hash: Mapped[str | None] = mapped_column(String(64))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    encrypted_push_subscription: Mapped[str | None] = mapped_column(Text)


class Notification(Entity):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_notification_event_user"),
        UniqueConstraint("user_id", "station_id", "source_key", name="uq_notification_source_user"),
        CheckConstraint("(event_id IS NULL) <> (source_key IS NULL)", name="ck_notification_source"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("alert_events.id", ondelete="CASCADE"), index=True
    )
    source_key: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    title: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(48))
    link: Mapped[str] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    routed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(Entity):
    __tablename__ = "notification_deliveries"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_notification_delivery_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16), default="alert")
    dedupe_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    notification_ids: Mapped[list] = mapped_column(JSON, default=list)
    encrypted_payload: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(48))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
