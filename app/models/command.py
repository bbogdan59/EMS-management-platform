from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity

# Starea "aplicata" a unei comenzi NU e setata la salvare -- doar un
# CommandEvent de tip "executed", venit din raportarea dispozitivului, o
# poate produce. Platforma nu e mecanismul de protectie electrica: dispozitivul
# local viitor valideaza si poate respinge orice comanda.


class Command(Entity):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("device_id", "idempotency_key", name="uq_command_idempotency"),
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_interval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_intervals.id"), nullable=True, index=True
    )

    type: Mapped[str] = mapped_column(String(48), nullable=False)  # CommandType
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False)  # CommandStatus
    author: Mapped[str] = mapped_column(String(24), nullable=False)  # optimizer|user|admin
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    device: Mapped[Device] = relationship()  # noqa: F821
    events: Mapped[list[CommandEvent]] = relationship(
        back_populates="command", cascade="all, delete-orphan", order_by="CommandEvent.created_at"
    )


class CommandEvent(Entity):
    """Jurnal imutabil al tranzitiilor de stare ale unei comenzi."""

    __tablename__ = "command_events"

    command_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("commands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)  # device|system|user
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    command: Mapped[Command] = relationship(back_populates="events")
