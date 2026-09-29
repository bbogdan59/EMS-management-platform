"""Mobile credentials are separate from browser cookies and device API keys."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class MobileSession(Entity):
    __tablename__ = "mobile_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    installation_id: Mapped[uuid.UUID]
    installation_key_hash: Mapped[str] = mapped_column(String(64))
    device_name: Mapped[str] = mapped_column(String(80))
    platform: Mapped[str] = mapped_column(String(16))
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MobileRefreshToken(Entity):
    __tablename__ = "mobile_refresh_tokens"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mobile_sessions.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MobileRegistration(Entity):
    __tablename__ = "mobile_registrations"

    email: Mapped[str] = mapped_column(String(320), unique=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(default=0)
