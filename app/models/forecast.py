from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity

# Prognozele meteo brute (radiatie, temperatura, vant) folosesc FLOAT: sunt
# marimi fizice continue, aproximative prin definitie, nu se insumeaza
# financiar si nu au nevoie de precizie zecimala exacta. Prognozele de
# putere/energie (PV, consum) folosesc NUMERIC ca sa fie comparabile direct
# cu valorile masurate (tot NUMERIC) in graficele "prognoza vs. realizat".


class WeatherForecast(Entity):
    __tablename__ = "weather_forecasts"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="open-meteo", nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ghi_w_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    dni_w_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    dhi_w_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_cover_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    confidence: Mapped[str] = mapped_column(String(16), default="nominal", nullable=False)  # nominal|low|high
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PvForecast(Entity):
    __tablename__ = "pv_forecasts"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="pvlib", nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    based_on_weather_forecast_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("weather_forecasts.id"), nullable=True
    )

    predicted_power_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    scenario: Mapped[str] = mapped_column(String(16), default="expected", nullable=False)  # p10|expected|p90
    confidence: Mapped[str] = mapped_column(String(16), default="nominal", nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ConsumptionForecast(Entity):
    __tablename__ = "consumption_forecasts"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="historical_profile", nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    base_load_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    ev_component_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0, nullable=False)
    flexible_component_kw: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0, nullable=False)

    is_cold_start: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, doc="True daca nu exista istoric suficient pt. profil pe ora/zi."
    )
    confidence: Mapped[str] = mapped_column(String(16), default="nominal", nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
