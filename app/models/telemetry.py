from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity

# Conventii de semn (documentate si in docs/API.md):
#   battery_power_w: pozitiv = incarcare, negativ = descarcare
#   grid_power_w:    pozitiv = import din retea, negativ = export in retea
#   pv_power_w, load_power_w, ev_power_w: intotdeauna >= 0


class TelemetryRaw(Entity):
    """Un mesaj de telemetrie primit de la un dispozitiv.

    Deduplicare: (device_id, boot_id, sequence) trebuie sa fie unic -- un
    dispozitiv care repeta o secventa dupa un restart foloseste un boot_id nou.
    """

    __tablename__ = "telemetry_raw"
    __table_args__ = (
        UniqueConstraint("device_id", "boot_id", "sequence", name="uq_telemetry_dedup"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pv_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    load_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    battery_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    grid_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    battery_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    ev_connected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ev_power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    quality_flags: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TelemetryAggregate(Entity):
    """Agregate energetice pe interval (15m/ora/zi/luna), folosite pentru
    grafice si retentie pe termen lung dupa expirarea datelor brute.

    Contract de integrare (vezi si `aggregation_service`): fiecare camp de
    energie e rezultatul unei integrari ponderate in timp a puterii
    instantanee (nu media aritmetica simpla a esantioanelor), pe convenția
    "zero-order hold" -- valoarea unui esantion se considera valabila de la
    momentul lui pana la urmatorul esantion cunoscut, dar NU mai mult de
    `aggregation_service.MAX_GAP_SECONDS`. Un camp e `NULL` daca metrica
    respectiva nu a avut NICIO acoperire in interval -- necunoscut nu
    inseamna niciodata zero. `coverage` retine, separat pe metrica, fractia
    din durata intervalului acoperita efectiv de date (0..1); un consumator
    poate decide singur ce prag de acoperire accepta.
    """

    __tablename__ = "telemetry_aggregates"
    __table_args__ = (
        UniqueConstraint("station_id", "period_type", "period_start", name="uq_telemetry_aggregate_period"),
    )

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)  # interval_15m|hour|day|month
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # NULL = metrica necunoscuta in acest interval (fara acoperire), NU zero.
    pv_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    load_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    battery_charge_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    battery_discharge_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    grid_import_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    grid_export_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    ev_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    # Explicit nullable: 0% SOC e o valoare reala (baterie goala), distincta
    # de "necunoscut" -- niciodata colapsate una in cealalta (bug corectat).
    avg_battery_soc_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_quality: Mapped[str] = mapped_column(String(16), default="measured", nullable=False)

    # Fractia din durata intervalului acoperita de date, per metrica -- chei:
    # "pv", "load", "battery", "grid", "ev", "soc". Absenta unei chei ==
    # acoperire 0 pentru acea metrica (camp NULL). Vezi docstring-ul clasei.
    coverage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
