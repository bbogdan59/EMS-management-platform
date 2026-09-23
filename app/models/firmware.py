"""OTA firmware fleet management (issue #168). "Firmware" here means the
EMS-device-code AGENT installed on the Raspberry Pi -- never the DEYE
inverter firmware and never a Raspberry Pi OS/kernel upgrade (explicitly out
of scope, see the issue). The device-side updater/verification/rollback is
implemented in the companion EMS-device-code#13; this module is the
platform-side release registry + rollout state machine that issues it a
structured update TARGET, never a shell command."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Entity
from app.models.enums import FirmwareReleaseStatus, FirmwareRolloutStatus


class FirmwareRelease(Entity):
    """Immutable-after-publish registry entry for one EMS-device-code build.
    "Immutable" is enforced at the service layer (firmware_service never lets
    a caller mutate the fields below once status=published) -- a correction
    is always a NEW release, never an edit of a published one. The artifact
    bytes themselves are never stored on Railway's ephemeral disk; only an
    opaque `storage_key` into the configured backend (see
    app/services/firmware_storage.py) lives here."""

    __tablename__ = "firmware_releases"

    version: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # FirmwareChannel
    hardware_platform: Mapped[str] = mapped_column(String(64), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    min_compatible_agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Never accepted without both -- see firmware_service.publish_release.
    sha256_hex: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_ed25519_hex: Mapped[str] = mapped_column(String(128), nullable=False)
    # Supports key rotation without breaking verification of older releases
    # signed under a previous key -- the PRIVATE key never appears in this
    # DB, only this opaque identifier (see firmware_signing.py).
    signing_key_id: Mapped[str] = mapped_column(String(64), nullable=False)

    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=FirmwareReleaseStatus.draft.value, nullable=False)

    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    rollouts: Mapped[list[FirmwareRollout]] = relationship(back_populates="release")


class FirmwareRollout(Entity):
    """One rollout campaign for a single published release. Concurrency
    limits, pause/resume, auto-pause-on-failure and cancellation are all
    controlled HERE, not per individual FirmwareDeployment -- a device-level
    action never has to guess campaign-wide policy."""

    __tablename__ = "firmware_rollouts"

    release_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("firmware_releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=FirmwareRolloutStatus.active.value, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # Auto-pause threshold: percentage of TERMINAL deployments (failed/
    # timed_out/rolled_back) in this rollout that trips an automatic pause
    # -- see firmware_service._maybe_auto_pause.
    failure_threshold_percent: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    # Downgrade is blocked by default (issue #168) -- an explicit rollout
    # with allow_downgrade=True must carry a reason; enforced together at
    # the service layer, not just a UI checkbox.
    allow_downgrade: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    downgrade_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auto_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_paused_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    release: Mapped[FirmwareRelease] = relationship(back_populates="rollouts")
    deployments: Mapped[list[FirmwareDeployment]] = relationship(
        back_populates="rollout", cascade="all, delete-orphan"
    )


class FirmwareDeployment(Entity):
    """One device's attempt at one release within a rollout. `status` is the
    state machine from FirmwareDeploymentStatus; every transition is also
    logged immutably in `events` (symmetric with Command/CommandEvent).
    Idempotent per (device, idempotency_key): re-requesting the same release
    for the same device returns the existing row instead of creating a
    duplicate offer (see firmware_service.request_deployment)."""

    __tablename__ = "firmware_deployments"
    __table_args__ = (
        UniqueConstraint("device_id", "idempotency_key", name="uq_firmware_deployment_idempotency"),
    )

    rollout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("firmware_rollouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firmware_releases.id"), nullable=False, index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # FirmwareDeploymentStatus
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    from_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    is_downgrade: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    offer_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set only once the device reports `restarting` -- the sweep task
    # (firmware_deployment_sweep_task) marks `timed_out` if no confirmation
    # arrives by this deadline. NULL before that point -- a deployment stuck
    # earlier in the pipeline (e.g. never even downloaded) times out via
    # `offer_expires_at` instead, not this field.
    confirmation_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Required to confirm the device actually restarted into new code, not
    # just resumed the old process (issue #168's "boot_id nou" requirement).
    boot_id_before: Mapped[str | None] = mapped_column(String(64), nullable=True)
    boot_id_after: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    rollout: Mapped[FirmwareRollout] = relationship(back_populates="deployments")
    release: Mapped[FirmwareRelease] = relationship()
    device: Mapped[Device] = relationship()  # noqa: F821
    events: Mapped[list[FirmwareDeploymentEvent]] = relationship(
        back_populates="deployment", cascade="all, delete-orphan", order_by="FirmwareDeploymentEvent.created_at"
    )


class FirmwareDeploymentEvent(Entity):
    """Immutable transition log for a FirmwareDeployment -- symmetric with
    CommandEvent. Never edited/deleted; the current `status` on the parent
    row is a projection of the last event, but this table is the audit
    trail an incident review actually reads."""

    __tablename__ = "firmware_deployment_events"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("firmware_deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # device|system|admin
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    deployment: Mapped[FirmwareDeployment] = relationship(back_populates="events")
