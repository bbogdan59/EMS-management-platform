from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity


class PreferenceVersion(Entity):
    """Preferintele clientului pentru o statie, versionate (istoric imutabil).

    Nu sunt modificate automat de sistem (nici de invatarea consumului) --
    doar un utilizator autorizat creeaza o versiune noua explicit.

    Distinctie obligatoriu vs. flexibil:
      - constrangeri OBLIGATORII (hard, niciodata incalcate INTENTIONAT de
        optimizator): max_optimization_energy_kwh, allow_grid_charge,
        allow_battery_export, max_efc_per_day/month, limitele tehnice din
        StationConfigVersion. min_reserve_soc_percent/max_normal_soc_percent
        sunt tot obligatorii pentru orice decizie NOUA a optimizatorului, dar
        cu o exceptie explicita: daca SOC-ul masurat la inceputul orizontului
        e deja in afara benzii, banda devine o tinta puternic penalizata (nu o
        limita fizica) exact pentru intervalul de recuperare -- altfel un SOC
        real in afara benzii ar face orice plan infezabil de la primul interval,
        iar optimizatorul ar fi tentat sa "corecteze" artificial SOC-ul masurat
        in loc sa raporteze corect starea si sa recupereze cat mai repede.
      - preferinte FLEXIBILE (soft, optimizatorul le urmareste dar poate devia
        cu penalizare daca intra in conflict cu o constrangere obligatorie sau
        cu obiectivul de cost): soc_targets, ev_required_energy_kwh/ev_departure_time,
        priority, arbitrage_min_benefit_lei.
    """

    __tablename__ = "preference_versions"
    __table_args__ = (UniqueConstraint("station_id", "version", name="uq_preference_version"),)

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # --- Constrangeri obligatorii ---
    min_reserve_soc_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=10, nullable=False)
    max_normal_soc_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=100, nullable=False)
    max_optimization_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(9, 3), nullable=True)
    allow_grid_charge: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_battery_export: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_efc_per_day: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    max_efc_per_month: Mapped[Decimal | None] = mapped_column(Numeric(7, 3), nullable=True)

    # --- Preferinte flexibile ---
    soc_targets: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
        doc=(
            "Lista de tinte SOC recurente: "
            '[{"time": "18:00", "days_of_week": [0,1,2,3,4], "target_soc_percent": 80}]'
        ),
    )
    priority: Mapped[str] = mapped_column(String(24), default="cost", nullable=False)  # OptimizationPriority
    ev_required_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    ev_departure_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    automation_suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    arbitrage_min_benefit_lei: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0, nullable=False)

    conflict_warnings: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False, doc="Conflicte detectate la salvare (explicatii RO)."
    )

    station: Mapped[Station] = relationship()  # noqa: F821


StationPreference = PreferenceVersion
