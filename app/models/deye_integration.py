from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity
from app.models.enums import DeyeCloudConnectionStatus


class DeyeCloudConnection(Entity):
    """Legatura dintre o statie a platformei si un cont Deye Cloud al
    clientului (issue #43) -- read-only, fara nicio scriere/comanda catre
    Deye. O statie are cel mult o conexiune Deye Cloud (constrangere unica).

    Credentialele reversibile (parola contului Deye Cloud al clientului,
    token-ul de acces cache-uit) sunt criptate la repaus (`app.core.crypto`,
    Fernet) -- niciodata in clar in baza de date sau in loguri. Parola trebuie
    pastrata (nu doar token-ul) pentru ca API-ul Deye Cloud documentat nu
    expune un endpoint separat de refresh -- vezi docs/LIMITATIONS.md."""

    __tablename__ = "deye_cloud_connections"
    __table_args__ = (UniqueConstraint("station_id", name="uq_deye_cloud_connection_station"),)

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=DeyeCloudConnectionStatus.pending_selection.value, nullable=False
    )
    region: Mapped[str] = mapped_column(String(8), default="eu", nullable=False)

    # Contul Deye Cloud al CLIENTULUI (nu appId/appSecret-ul platformei, care
    # vine din `Settings` si e comun tuturor conexiunilor).
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)
    encrypted_account_password: Mapped[str] = mapped_column(String(500), nullable=False)

    # Setat abia dupa ce clientul alege, dintre statiile contului sau Deye
    # Cloud, pe care sa o lege de aceasta statie a platformei (un cont poate
    # avea mai multe statii Deye Cloud).
    remote_station_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    remote_station_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Snapshot minim (doar id/nume) al listei obtinute la autentificare.
    # Evita apeluri Deye din GET-ul paginii si permite validarea server-side
    # a selectiei, fara a avea incredere in hidden inputs controlate de client.
    pending_remote_stations: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    # Device-ul SINTETIC (vezi `app.models.device.Device`) creat pentru a
    # atasa randurile `TelemetryRaw` importate din Deye Cloud -- nu reprezinta
    # un dispozitiv fizic RS485, nu accepta niciodata comenzi/config.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )

    consent_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Cache-ul token-ului de acces (evita re-autentificarea cu parola la
    # fiecare polling -- reautentificarea propriu-zisa ramane necesara cand
    # expira, din lipsa unui grant de tip refresh_token documentat).
    encrypted_access_token: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # succeeded|failed|skipped
    last_sync_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    station: Mapped[Station] = relationship()  # noqa: F821
    device: Mapped[Device | None] = relationship()  # noqa: F821
    device_links: Mapped[list[DeyeCloudDeviceLink]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class DeyeCloudDeviceLink(Entity):
    """Inventarul (metadata) dispozitivelor raportate de Deye Cloud pentru
    statia legata -- IMPORTAT o singura data la selectia statiei, doar pentru
    afisare ("ce hardware e la aceasta statie in cloud"). Telemetria propriu
    zisa e ingerata la nivel de STATIE (`/station/latest`), nu per-device --
    vezi docs/LIMITATIONS.md pentru motiv (nu am putut verifica live numele
    exacte ale punctelor de masura per tip de dispozitiv)."""

    __tablename__ = "deye_cloud_device_links"
    __table_args__ = (
        UniqueConstraint("connection_id", "remote_device_sn", name="uq_deye_cloud_device_link"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deye_cloud_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remote_device_sn: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_device_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    connection: Mapped[DeyeCloudConnection] = relationship(back_populates="device_links")
