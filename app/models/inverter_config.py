from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class InverterProfile(Entity):
    __tablename__ = 'inverter_profiles'
    digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    approved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), nullable=False)


class InverterDesired(Entity):
    __tablename__ = 'inverter_desired'
    __table_args__ = (UniqueConstraint('device_id', 'version'), UniqueConstraint('device_id', 'request_id'))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('devices.id'), nullable=False, index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('inverter_profiles.id'), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    connection: Mapped[dict] = mapped_column(JSON, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), nullable=False)
    command_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('commands.id'), nullable=True)


class InverterReport(Entity):
    __tablename__ = 'inverter_reports'
    __table_args__ = (UniqueConstraint('device_id', 'version'), UniqueConstraint('device_id', 'request_id'))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('devices.id'), nullable=False, index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('inverter_profiles.id'), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    desired_version: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    firmware: Mapped[str] = mapped_column(String(100), nullable=False)
