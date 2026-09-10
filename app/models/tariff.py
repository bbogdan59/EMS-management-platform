from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity

# Banii sunt stocati ca NUMERIC (Decimal), niciodata float: evitam erorile de
# rotunjire binar la insumarea a mii de intervale de 15 minute pe luni de zile,
# care ar produce diferente vizibile in facturi/rapoarte. Energia (kWh) e tot
# NUMERIC pentru acelasi motiv -- se aduna in agregate si trebuie sa fie
# reproductibila bit-cu-bit indiferent de ordinea insumarii.


class Tariff(Entity):
    """Contract tarifar pentru o statie, pe o directie (import/export)."""

    __tablename__ = "tariffs"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # import|export
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # fixed|indexed_opcom
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    station: Mapped[Station] = relationship()  # noqa: F821
    versions: Mapped[list[TariffVersion]] = relationship(
        back_populates="tariff", cascade="all, delete-orphan", order_by="TariffVersion.valid_from"
    )


class TariffVersion(Entity):
    """O versiune cu valabilitate determinata a unui tarif.

    Pentru `kind=fixed`: se foloseste `fixed_price_lei_per_kwh`.
    Pentru `kind=indexed_opcom`: pretul efectiv = pret_OPCOM + `opcom_margin_lei_per_kwh`
    (marja poate fi negativa). Daca formula contractuala reala nu e implementata
    (ex. formule cu componente reglementate variabile complexe), seteaza
    `economic_calculation_disabled=True` si documenteaza in `limitation_note`.
    """

    __tablename__ = "tariff_versions"

    tariff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tariffs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fixed_price_lei_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 5), nullable=True)
    opcom_margin_lei_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 5), nullable=True)

    fixed_monthly_fee_lei: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    variable_component_lei_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0, nullable=False)

    settlement_method: Mapped[str] = mapped_column(String(64), default="net_metering_15min", nullable=False)
    settlement_interval_days: Mapped[int] = mapped_column(default=30, nullable=False)

    economic_calculation_disabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    limitation_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    tariff: Mapped[Tariff] = relationship(back_populates="versions")
