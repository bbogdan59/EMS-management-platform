from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
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


class ImportRun(Entity):
    """O incercare de import al preturilor PZU (OPCOM) pentru o zi de livrare.

    Import idempotent: o noua rulare reusita pentru aceeasi `delivery_date`
    creeaza o noua `revision`; revizia anterioara ramane in baza (audit),
    dar doar ultima e "curenta" pentru afisare/optimizare.
    """

    __tablename__ = "import_runs"

    source: Mapped[str] = mapped_column(String(32), default="opcom_pzu", nullable=False)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ImportRunStatus
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_synthetic_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interval_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "delivery_date", "revision", name="uq_import_run_revision"),
    )

    intervals: Mapped[list[MarketPriceInterval]] = relationship(
        back_populates="import_run", cascade="all, delete-orphan"
    )


class MarketPriceInterval(Entity):
    """Pret PZU pe un interval de decontare (de regula 15 minute), UTC.

    `price_lei_per_mwh` este valoarea publicata (asa cum apare in CSV);
    `price_lei_per_kwh` e conversia /1000 folosita in restul aplicatiei.
    """

    __tablename__ = "market_price_intervals"
    __table_args__ = (
        UniqueConstraint(
            "source", "delivery_date", "interval_start", "revision", name="uq_market_price_interval"
        ),
    )

    import_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), default="opcom_pzu", nullable=False)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    interval_index: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    currency: Mapped[str] = mapped_column(String(8), default="RON", nullable=False)
    price_lei_per_mwh: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    price_lei_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    is_negative: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    import_run: Mapped[ImportRun] = relationship(back_populates="intervals")
