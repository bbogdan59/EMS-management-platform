from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity
from app.models.enums import ClaimCodeStatus, DeviceStatus


class ClaimCode(Entity):
    """Cod unic, cu expirare, generat de un operator/admin pentru a asocia un
    dispozitiv fizic (viitor) unei statii. Dovada de posesie = codul insusi,
    transmis o singura data prin canal separat (UI) catre instalator."""

    __tablename__ = "claim_codes"

    station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    code_prefix: Mapped[str] = mapped_column(String(8), nullable=False)  # afisat in UI pt identificare
    status: Mapped[str] = mapped_column(String(16), default=ClaimCodeStatus.pending.value, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id"), nullable=True
    )

    station: Mapped[Station] = relationship()  # noqa: F821


class Device(Entity):
    """Un dispozitiv poate exista fara statie: un enrollment automat (vezi
    `installation_uuid`/`provisioning_secret_hash` mai jos) creeaza device-ul
    in starea `pending_claim`, FARA statie, pana cand un administrator il
    aloca explicit -- `station_id` e deci nullable. Codul de asociere clasic
    (`ClaimCode`, generat de operator PENTRU o statie anume) creeaza in
    continuare device-ul direct cu statia setata, ca inainte."""

    __tablename__ = "devices"

    station_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=DeviceStatus.pending_claim.value, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_boot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Enrollment automat (issue #16), independent de ClaimCode ---
    # Identitate DECLARATA de dispozitiv la primul contact -- niciodata
    # folosita pentru a alege/autoriza tenant-ul/statia (vezi
    # device_service.enroll_device si docs/API.md). Unica per instalare
    # fizica; NU e un secret -- e doar cheia de corelare pentru operator.
    installation_uuid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # Public inventory identifier printed on the label. It is deliberately
    # separate from both authentication secrets below.
    serial_number: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    # SHA-256 of the high-entropy, sealed package Device Code. Cleared after
    # the customer claims it, making the code one-use without retaining it.
    activation_code_hash: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    activation_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hash-ul secretului de provisioning generat/detinut de dispozitiv (nu de
    # server) -- dovada de posesie la fiecare reincercare idempotenta a
    # enrollment-ului, fara sa fie nevoie sa retransmitem un secret emis de noi.
    provisioning_secret_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Secretul de credentiala NOU generat la alocare, pastrat in clar DOAR
    # pana cand dispozitivul il foloseste cu succes prima data (vezi
    # device_service.mark_bootstrap_credential_delivered) -- fereastra scurta,
    # necesara ca raspunsul de alocare pierdut sa fie recuperabil idempotent
    # fara sa retrimitem un secret pe care nu-l mai avem in clar altfel.
    pending_credential_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allocated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    station: Mapped[Station | None] = relationship()  # noqa: F821
    credentials: Mapped[list[DeviceCredential]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class DeviceCredential(Entity):
    """Credentiala individuala a dispozitivului (secret hash-uit), rotativa si
    revocabila independent de dispozitiv (permite rotatie fara re-asociere)."""

    __tablename__ = "device_credentials"

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(back_populates="credentials")
