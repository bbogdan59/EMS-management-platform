from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity


class Station(Entity):
    """O statie = locatia energetica a unui client: PV + invertor + baterie +
    consum casnic + (optional) EV. Apartine unei singure organizatii."""

    __tablename__ = "stations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Bucharest", nullable=False)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), default="shadow", nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="stations")  # noqa: F821
    panel_groups: Mapped[list[PanelGroup]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    config_versions: Mapped[list[StationConfigVersion]] = relationship(
        back_populates="station", cascade="all, delete-orphan", order_by="StationConfigVersion.version"
    )


class StationConfigVersion(Entity):
    """Configuratie tehnica a statiei, versionata (istoric complet, imutabil).

    O noua editare creeaza o versiune noua; `StationConfig` este un view
    convenabil pe ultima versiune activa.
    """

    __tablename__ = "station_config_versions"
    __table_args__ = (UniqueConstraint("station_id", "version", name="uq_station_config_version"),)

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Putere instalata / invertor -- kW, precizie 0.01 kW suficienta pt. echipamente.
    pv_installed_power_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    inverter_power_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)

    # Baterie: capacitate de referinta (nameplate, la instalare) vs. disponibila
    # (poate scadea in timp din degradare) -- separate explicit, cf. cerinta EFC.
    battery_reference_capacity_kwh: Mapped[Decimal | None] = mapped_column(Numeric(9, 3), nullable=True)
    battery_available_capacity_kwh: Mapped[Decimal | None] = mapped_column(Numeric(9, 3), nullable=True)
    battery_max_charge_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    battery_max_discharge_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    battery_charge_efficiency: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    battery_discharge_efficiency: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    grid_import_limit_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    grid_export_limit_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)

    ev_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ev_battery_capacity_kwh: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    ev_max_charge_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)

    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    station: Mapped[Station] = relationship(back_populates="config_versions")
    panel_groups: Mapped[list[PanelGroup]] = relationship(
        back_populates="config_version", cascade="all, delete-orphan"
    )


# Alias de conventie folosit in restul codului (ultima versiune = "config curent").
StationConfig = StationConfigVersion


class PanelGroup(Entity):
    """Grup de panouri cu orientare/inclinatie proprii, folosit de modelul PV (pvlib)."""

    __tablename__ = "panel_groups"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("station_config_versions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    power_kwp: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    azimuth_degrees: Mapped[Decimal] = mapped_column(Numeric(5, 1), nullable=False)  # 0=N,90=E,180=S,270=V
    tilt_degrees: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)

    station: Mapped[Station] = relationship(back_populates="panel_groups")
    config_version: Mapped[StationConfigVersion] = relationship(back_populates="panel_groups")
