"""OTA firmware fleet management (issue #168). "Firmware" here is strictly
the EMS-device-code AGENT installed on the Raspberry Pi -- never DEYE
inverter firmware and never a Raspberry Pi OS/kernel upgrade (explicitly
out of scope). The device-side updater/verification/rollback lives in the
companion EMS-device-code#13; this module owns the release registry and
the per-device deployment state machine, and only ever hands a device a
strictly-typed `release_id`/`manifest_id` target -- never a shell command,
a raw URL, or executable arguments.

RBAC is enforced by the CALLER (web routes/API deps), not here, matching
this codebase's existing convention (see app/api/deps.py) -- this module
only enforces release/device/rollout STATE invariants."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import expires_in, utcnow
from app.models.device import Device
from app.models.enums import FirmwareDeploymentStatus, FirmwareReleaseStatus, FirmwareRolloutStatus
from app.models.firmware import (
    FirmwareDeployment,
    FirmwareDeploymentEvent,
    FirmwareRelease,
    FirmwareRollout,
)
from app.models.user import User
from app.services import firmware_signing, firmware_storage

# Statuses in which a deployment is actively occupying a rollout's
# concurrency slot -- used both to cap new offers and by the fleet UI's
# "in curs" badge.
IN_FLIGHT_STATUSES = {
    FirmwareDeploymentStatus.offered.value,
    FirmwareDeploymentStatus.downloading.value,
    FirmwareDeploymentStatus.verified.value,
    FirmwareDeploymentStatus.installing.value,
    FirmwareDeploymentStatus.awaiting_confirmation.value,
}
TERMINAL_STATUSES = {
    FirmwareDeploymentStatus.succeeded.value,
    FirmwareDeploymentStatus.rejected.value,
    FirmwareDeploymentStatus.failed.value,
    FirmwareDeploymentStatus.timed_out.value,
    FirmwareDeploymentStatus.rolled_back.value,
    FirmwareDeploymentStatus.cancelled.value,
}
TERMINAL_FAILURE_STATUSES = {
    FirmwareDeploymentStatus.failed.value,
    FirmwareDeploymentStatus.timed_out.value,
    FirmwareDeploymentStatus.rolled_back.value,
}
# Device-reported event_type -> the status it moves a deployment TO. Only
# these forward transitions are ever accepted from the device; anything
# else (including going "backwards") is rejected explicitly, never
# silently coerced.
_FORWARD_EVENTS = {
    FirmwareDeploymentStatus.offered.value: {
        "downloading": FirmwareDeploymentStatus.downloading.value,
        "rejected": FirmwareDeploymentStatus.rejected.value,
    },
    FirmwareDeploymentStatus.downloading.value: {
        "verified": FirmwareDeploymentStatus.verified.value,
        "failed": FirmwareDeploymentStatus.failed.value,
    },
    FirmwareDeploymentStatus.verified.value: {
        "installing": FirmwareDeploymentStatus.installing.value,
        "failed": FirmwareDeploymentStatus.failed.value,
    },
    FirmwareDeploymentStatus.installing.value: {
        # "restarting" is the last message before the device's own process
        # exits into the new build -- the platform's perspective from this
        # point on is "waiting for confirmation", so the STATUS column jumps
        # straight to awaiting_confirmation (the event_type recorded in the
        # immutable event log stays "restarting" for audit granularity).
        "restarting": FirmwareDeploymentStatus.awaiting_confirmation.value,
        "failed": FirmwareDeploymentStatus.failed.value,
    },
    FirmwareDeploymentStatus.awaiting_confirmation.value: {
        "confirmed": None,  # resolved dynamically: succeeded or rolled_back, see report_deployment_event
        "failed": FirmwareDeploymentStatus.failed.value,
    },
}


class FirmwareServiceError(Exception):
    pass


# --- Version compatibility (best-effort heuristic, documented as such) ---


def _version_tuple(version: str) -> tuple[int, ...]:
    """Numeric-chunk comparison, not a full semver parser -- good enough to
    order "1.4.0" < "1.10.0" and to detect a downgrade; a non-numeric
    suffix (e.g. "1.4.0-rc1") is ignored for ordering purposes."""
    parts = []
    for chunk in version.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_downgrade(current_version: str | None, target_version: str) -> bool:
    if not current_version:
        return False  # unknown baseline: never blocked as a downgrade, but see _compatibility_reason for min-version gating
    return _version_tuple(target_version) < _version_tuple(current_version)


def _compatibility_reason(device: Device, release: FirmwareRelease) -> str | None:
    """None = compatible. Otherwise a short machine-readable reason the
    caller can surface to the admin, per issue #168's requirement that
    platform/architecture/version/protocol compatibility is checked BEFORE
    any offer is made."""
    if not device.hardware_platform or device.hardware_platform != release.hardware_platform:
        return "hardware_platform_mismatch"
    if not device.architecture or device.architecture != release.architecture:
        return "architecture_mismatch"
    if release.min_compatible_agent_version:
        if not device.firmware_version:
            return "unknown_agent_version"
        if _version_tuple(device.firmware_version) < _version_tuple(release.min_compatible_agent_version):
            return "agent_version_too_old"
    return None


# --- Release registry -----------------------------------------------------


def build_storage_key(version: str, sha256_hex: str) -> str:
    # Server-generated, opaque, never derived from unsanitized user input.
    safe_version = "".join(ch for ch in version if ch.isalnum() or ch in ".-_") or "unknown"
    return f"releases/{safe_version}/{sha256_hex[:16]}.tar.gz"


def create_release(
    db: Session,
    user: User,
    *,
    version: str,
    channel: str,
    hardware_platform: str,
    architecture: str,
    protocol_schema_version: int,
    artifact_bytes: bytes,
    min_compatible_agent_version: str | None = None,
    release_notes: str | None = None,
    storage: firmware_storage.ReleaseStorage | None = None,
) -> FirmwareRelease:
    """Creates a `draft` release with its artifact already uploaded, hashed
    and signed -- draft exists as a review/undo window before `publish_release`
    makes the metadata immutable, not as a place to attach the artifact later."""
    settings = get_settings()
    version = version.strip()
    if not version or len(version) > 32:
        raise FirmwareServiceError("Versiunea trebuie sa aiba intre 1 si 32 de caractere.")
    if not artifact_bytes:
        raise FirmwareServiceError("Artifactul nu poate fi gol.")
    if len(artifact_bytes) > settings.firmware_max_artifact_bytes:
        raise FirmwareServiceError(
            f"Artifactul depaseste limita configurata ({settings.firmware_max_artifact_bytes} bytes)."
        )
    if db.scalar(select(FirmwareRelease.id).where(FirmwareRelease.version == version)) is not None:
        raise FirmwareServiceError(f"Exista deja un release cu versiunea '{version}'.")

    digest = firmware_storage.sha256_hex(artifact_bytes)
    try:
        signature = firmware_signing.sign_hex(artifact_bytes, private_key_pem=settings.firmware_signing_private_key_pem)
    except firmware_signing.FirmwareSigningError as exc:
        raise FirmwareServiceError(str(exc)) from exc
    key = build_storage_key(version, digest)

    store = storage if storage is not None else firmware_storage.get_release_storage(settings)
    try:
        store.put(key, artifact_bytes, content_type="application/gzip")
    except firmware_storage.ReleaseStorageError as exc:
        raise FirmwareServiceError(str(exc)) from exc

    release = FirmwareRelease(
        version=version,
        channel=channel,
        hardware_platform=hardware_platform,
        architecture=architecture,
        protocol_schema_version=protocol_schema_version,
        min_compatible_agent_version=min_compatible_agent_version,
        storage_key=key,
        artifact_size_bytes=len(artifact_bytes),
        sha256_hex=digest,
        signature_ed25519_hex=signature,
        signing_key_id=settings.firmware_signing_key_id,
        release_notes=release_notes,
        status=FirmwareReleaseStatus.draft.value,
        created_by_user_id=user.id,
    )
    db.add(release)
    db.flush()
    return release


def publish_release(db: Session, release: FirmwareRelease) -> FirmwareRelease:
    if release.status != FirmwareReleaseStatus.draft.value:
        raise FirmwareServiceError(f"Release-ul este in starea '{release.status}', nu poate fi publicat.")
    release.status = FirmwareReleaseStatus.published.value
    release.published_at = utcnow()
    db.add(release)
    db.flush()
    return release


def revoke_release(db: Session, release: FirmwareRelease, *, reason: str) -> FirmwareRelease:
    if release.status == FirmwareReleaseStatus.revoked.value:
        raise FirmwareServiceError("Release-ul este deja revocat.")
    if not reason or not reason.strip():
        raise FirmwareServiceError("Motivul revocarii este obligatoriu.")
    release.status = FirmwareReleaseStatus.revoked.value
    release.revoked_at = utcnow()
    release.revoked_reason = reason.strip()[:500]
    db.add(release)

    # Cannot retroactively un-install already-succeeded devices -- only
    # cancels what hasn't started consuming the artifact yet.
    not_yet_started = db.scalars(
        select(FirmwareDeployment).where(
            FirmwareDeployment.release_id == release.id,
            FirmwareDeployment.status.in_(
                [FirmwareDeploymentStatus.requested.value, FirmwareDeploymentStatus.offered.value]
            ),
        )
    ).all()
    for deployment in not_yet_started:
        deployment.status = FirmwareDeploymentStatus.cancelled.value
        db.add(deployment)
        db.add(
            FirmwareDeploymentEvent(
                deployment_id=deployment.id, event_type="cancelled", source="system",
                message="Release revocat.",
            )
        )
    db.flush()
    return release


def preview_rollout(release: FirmwareRelease, devices: list[Device]) -> dict:
    """Read-only: the SAME eligibility logic `create_rollout` uses, without
    persisting anything -- issue #168's "confirmare inainte de rollout cu
    numarul device-urilor, versiunile curente/tinta si incompatibilitatile"."""
    eligible = []
    skipped: dict[str, str] = {}
    for device in devices:
        reason = _compatibility_reason(device, release)
        if reason is not None:
            skipped[str(device.id)] = reason
            continue
        downgrade = is_downgrade(device.firmware_version, release.version)
        eligible.append({"device": device, "is_downgrade": downgrade, "from_version": device.firmware_version})
    return {"eligible": eligible, "skipped": skipped}


# --- Rollouts ---------------------------------------------------------


def create_rollout(
    db: Session,
    user: User,
    release: FirmwareRelease,
    devices: list[Device],
    *,
    max_concurrent: int = 5,
    failure_threshold_percent: int = 20,
    allow_downgrade: bool = False,
    downgrade_reason: str | None = None,
) -> dict:
    """Returns {"rollout": FirmwareRollout, "created": [...], "skipped": {device_id: reason}}.
    A device skipped for incompatibility/downgrade is not an error for the
    whole rollout -- the caller (admin route) surfaces the per-device reasons."""
    if release.status != FirmwareReleaseStatus.published.value:
        raise FirmwareServiceError("Doar un release publicat poate porni un rollout.")
    if max_concurrent < 1:
        raise FirmwareServiceError("max_concurrent trebuie sa fie cel putin 1.")
    if not 0 <= failure_threshold_percent <= 100:
        raise FirmwareServiceError("failure_threshold_percent trebuie sa fie intre 0 si 100.")
    if allow_downgrade and not (downgrade_reason and downgrade_reason.strip()):
        raise FirmwareServiceError("Un rollout cu allow_downgrade necesita un motiv explicit.")

    rollout = FirmwareRollout(
        release_id=release.id,
        status=FirmwareRolloutStatus.active.value,
        created_by_user_id=user.id,
        max_concurrent=max_concurrent,
        failure_threshold_percent=failure_threshold_percent,
        allow_downgrade=allow_downgrade,
        downgrade_reason=(downgrade_reason.strip()[:500] if downgrade_reason else None),
    )
    db.add(rollout)
    db.flush()

    created: list[FirmwareDeployment] = []
    skipped: dict[str, str] = {}
    for device in devices:
        reason = _compatibility_reason(device, release)
        if reason is not None:
            skipped[str(device.id)] = reason
            continue
        downgrade = is_downgrade(device.firmware_version, release.version)
        if downgrade and not allow_downgrade:
            skipped[str(device.id)] = "downgrade_blocked"
            continue
        deployment = request_deployment(db, user, rollout, release, device, is_downgrade=downgrade)
        created.append(deployment)

    _promote_queued(db, rollout)
    db.flush()
    return {"rollout": rollout, "created": created, "skipped": skipped}


def request_deployment(
    db: Session, user: User, rollout: FirmwareRollout, release: FirmwareRelease, device: Device, *, is_downgrade: bool
) -> FirmwareDeployment:
    """Idempotent per (device, release): a repeat request for a release the
    device already has an active or successful deployment for returns that
    row unchanged. A repeat request after a TERMINAL FAILURE creates a new
    attempt (this is how an admin retries), never silently overwrites the
    failed row's history. `idempotency_key` is per-ATTEMPT (the DB unique
    constraint is what actually prevents a duplicate row from a concurrent
    double-call of this function for the same attempt); the "same release,
    any attempt" lookup below is a separate query, not the constraint."""
    existing = db.scalar(
        select(FirmwareDeployment)
        .where(FirmwareDeployment.device_id == device.id, FirmwareDeployment.release_id == release.id)
        .order_by(FirmwareDeployment.attempt.desc())
        .limit(1)
    )
    if existing is not None and existing.status not in TERMINAL_FAILURE_STATUSES:
        return existing

    settings = get_settings()
    now = utcnow()
    attempt = existing.attempt + 1 if existing is not None else 1
    deployment = FirmwareDeployment(
        rollout_id=rollout.id,
        release_id=release.id,
        device_id=device.id,
        idempotency_key=f"firmware:{release.id}:attempt:{attempt}",
        status=FirmwareDeploymentStatus.requested.value,
        attempt=attempt,
        from_version=device.firmware_version,
        target_version=release.version,
        is_downgrade=is_downgrade,
        requested_by_user_id=user.id,
        requested_at=now,
        offer_expires_at=expires_in(minutes=settings.firmware_deployment_offer_ttl_minutes),
    )
    db.add(deployment)
    db.flush()
    db.add(
        FirmwareDeploymentEvent(
            deployment_id=deployment.id, event_type="requested", source="admin",
            payload={"from_version": deployment.from_version, "target_version": deployment.target_version},
        )
    )
    db.flush()
    return deployment


def _promote_queued(db: Session, rollout: FirmwareRollout) -> int:
    """Advances `requested` deployments to `offered` up to the rollout's
    concurrency limit -- called after creating deployments and periodically
    by firmware_deployment_sweep_task as in-flight slots free up."""
    if rollout.status != FirmwareRolloutStatus.active.value:
        return 0
    in_flight_count = db.scalar(
        select(func.count(FirmwareDeployment.id)).where(
            FirmwareDeployment.rollout_id == rollout.id, FirmwareDeployment.status.in_(IN_FLIGHT_STATUSES)
        )
    ) or 0

    slots = rollout.max_concurrent - in_flight_count
    if slots <= 0:
        return 0
    queued = db.scalars(
        select(FirmwareDeployment)
        .where(FirmwareDeployment.rollout_id == rollout.id, FirmwareDeployment.status == FirmwareDeploymentStatus.requested.value)
        .order_by(FirmwareDeployment.requested_at)
        .limit(slots)
    ).all()
    settings = get_settings()
    promoted = 0
    for deployment in queued:
        deployment.status = FirmwareDeploymentStatus.offered.value
        deployment.offer_expires_at = expires_in(minutes=settings.firmware_deployment_offer_ttl_minutes)
        db.add(deployment)
        db.add(FirmwareDeploymentEvent(deployment_id=deployment.id, event_type="offered", source="system"))
        promoted += 1
    if promoted:
        db.flush()
    return promoted


def pause_rollout(db: Session, rollout: FirmwareRollout) -> FirmwareRollout:
    if rollout.status != FirmwareRolloutStatus.active.value:
        raise FirmwareServiceError(f"Rollout-ul este in starea '{rollout.status}', nu poate fi pus pe pauza.")
    rollout.status = FirmwareRolloutStatus.paused.value
    db.add(rollout)
    db.flush()
    return rollout


def resume_rollout(db: Session, rollout: FirmwareRollout) -> FirmwareRollout:
    if rollout.status != FirmwareRolloutStatus.paused.value:
        raise FirmwareServiceError(f"Rollout-ul este in starea '{rollout.status}', nu poate fi reluat.")
    rollout.status = FirmwareRolloutStatus.active.value
    rollout.auto_paused_at = None
    rollout.auto_paused_reason = None
    db.add(rollout)
    db.flush()
    _promote_queued(db, rollout)
    return rollout


def cancel_rollout(db: Session, rollout: FirmwareRollout, *, reason: str) -> FirmwareRollout:
    if rollout.status in (FirmwareRolloutStatus.completed.value, FirmwareRolloutStatus.cancelled.value):
        raise FirmwareServiceError(f"Rollout-ul este deja in starea '{rollout.status}'.")
    rollout.status = FirmwareRolloutStatus.cancelled.value
    rollout.cancelled_at = utcnow()
    db.add(rollout)

    # A device already mid-install (restarting/awaiting_confirmation) is
    # NOT force-cancelled -- it committed to an install the platform can no
    # longer safely recall; it must run to its own terminal state.
    cancellable = db.scalars(
        select(FirmwareDeployment).where(
            FirmwareDeployment.rollout_id == rollout.id,
            FirmwareDeployment.status.in_(
                [
                    FirmwareDeploymentStatus.requested.value, FirmwareDeploymentStatus.offered.value,
                    FirmwareDeploymentStatus.downloading.value, FirmwareDeploymentStatus.verified.value,
                    FirmwareDeploymentStatus.installing.value,
                ]
            ),
        )
    ).all()
    for deployment in cancellable:
        deployment.status = FirmwareDeploymentStatus.cancelled.value
        db.add(deployment)
        db.add(
            FirmwareDeploymentEvent(
                deployment_id=deployment.id, event_type="cancelled", source="admin", message=reason,
            )
        )
    db.flush()
    return rollout


def _maybe_auto_pause(db: Session, rollout: FirmwareRollout) -> bool:
    """Auto-pauses an active rollout once the percentage of TERMINAL-FAILURE
    deployments (failed/timed_out/rolled_back) among all deployments so far
    exceeds the configured threshold -- issue #168's "oprire automata la un
    prag de esec"."""
    if rollout.status != FirmwareRolloutStatus.active.value:
        return False
    all_deployments = db.scalars(
        select(FirmwareDeployment.status).where(FirmwareDeployment.rollout_id == rollout.id)
    ).all()
    if not all_deployments:
        return False
    failures = sum(1 for status in all_deployments if status in TERMINAL_FAILURE_STATUSES)
    if failures / len(all_deployments) * 100 < rollout.failure_threshold_percent:
        return False
    rollout.status = FirmwareRolloutStatus.paused.value
    rollout.auto_paused_at = utcnow()
    rollout.auto_paused_reason = (
        f"Prag de esec depasit: {failures}/{len(all_deployments)} implementari terminate cu esec."
    )
    db.add(rollout)
    db.flush()
    return True


def offer_payload(db: Session, deployment: FirmwareDeployment, storage: firmware_storage.ReleaseStorage) -> dict:
    """Builds the device-facing offer payload for one deployment -- a
    strictly-typed target (release_id, hash, signature, a time-limited
    download URL), never a shell command or an arbitrary URL."""
    settings = get_settings()
    release = db.get(FirmwareRelease, deployment.release_id)
    return {
        "deployment_id": deployment.id,
        "release_id": deployment.release_id,
        "target_version": deployment.target_version,
        "channel": release.channel,
        "status": deployment.status,
        "is_downgrade": deployment.is_downgrade,
        "offer_expires_at": deployment.offer_expires_at,
        "download_url": storage.generate_download_url(
            release.storage_key, ttl_seconds=settings.firmware_download_url_ttl_seconds
        ),
        "sha256_hex": release.sha256_hex,
        "signature_ed25519_hex": release.signature_ed25519_hex,
        "signing_key_id": release.signing_key_id,
        "artifact_size_bytes": release.artifact_size_bytes,
    }


# --- Device-facing: offer + progress reporting ---------------------------


def get_current_offer(db: Session, device: Device) -> FirmwareDeployment | None:
    """The single non-terminal deployment for this device, if any -- lets a
    device reconnect/resume mid-flow instead of only ever seeing a fresh
    offer. Expires an `offered` deployment past its offer window before
    returning (mirrors device_service.list_pending_commands's expire-then-list)."""
    now = utcnow()
    active = db.scalars(
        select(FirmwareDeployment)
        .where(FirmwareDeployment.device_id == device.id, FirmwareDeployment.status.in_(IN_FLIGHT_STATUSES))
        .order_by(FirmwareDeployment.requested_at.desc())
    ).all()

    for deployment in active:
        if deployment.status == FirmwareDeploymentStatus.offered.value and deployment.offer_expires_at < now:
            deployment.status = FirmwareDeploymentStatus.timed_out.value
            db.add(deployment)
            db.add(
                FirmwareDeploymentEvent(
                    deployment_id=deployment.id, event_type="timed_out", source="system",
                    message="Oferta a expirat inainte de a fi preluata.",
                )
            )
            continue
        db.flush()
        return deployment
    db.flush()
    return None


def report_deployment_event(
    db: Session, device: Device, deployment_id: uuid.UUID, event_type: str, *, payload: dict, message: str | None
) -> FirmwareDeployment:
    deployment = db.get(FirmwareDeployment, deployment_id)
    if deployment is None or deployment.device_id != device.id:
        raise FirmwareServiceError("Implementarea nu exista pentru acest dispozitiv.")

    if deployment.status in TERMINAL_STATUSES:
        # Idempotent retry: the SAME terminal event reported again (network
        # retry after a lost response) is a silent success, not an error --
        # mirrors device_service.report_command_result's contradiction check.
        last_event = db.scalar(
            select(FirmwareDeploymentEvent)
            .where(FirmwareDeploymentEvent.deployment_id == deployment.id, FirmwareDeploymentEvent.event_type == event_type)
            .order_by(FirmwareDeploymentEvent.created_at.desc())
            .limit(1)
        )
        if last_event is not None and last_event.payload == (payload or {}) and last_event.message == message:
            return deployment
        raise FirmwareServiceError(
            f"Implementarea este deja in starea '{deployment.status}'; reincercarea raporteaza un rezultat contradictoriu."
        )

    allowed = _FORWARD_EVENTS.get(deployment.status, {})
    if event_type not in allowed:
        raise FirmwareServiceError(
            f"Tranzitia '{event_type}' nu este permisa din starea '{deployment.status}'."
        )

    now = utcnow()
    if event_type == "restarting":
        settings = get_settings()
        boot_id = (payload or {}).get("boot_id")
        if not boot_id:
            raise FirmwareServiceError("Evenimentul 'restarting' necesita boot_id-ul curent (inainte de repornire).")
        deployment.boot_id_before = boot_id
        deployment.confirmation_deadline_at = now + timedelta(
            minutes=settings.firmware_deployment_confirmation_timeout_minutes
        )
        deployment.status = FirmwareDeploymentStatus.awaiting_confirmation.value
    elif event_type == "confirmed":
        deployment.status = _resolve_confirmation(deployment, payload or {})
        # This confirm call is itself an authenticated live contact (proving
        # exactly which version is actually running post-restart) -- update
        # the device's known version immediately rather than waiting for its
        # next heartbeat cycle. Applies whether it succeeded or rolled back:
        # `payload["version"]` is always the version now actually running.
        device.firmware_version = payload.get("version") or device.firmware_version
        device.firmware_version_source = "ota_confirmation"
        device.firmware_reported_at = now
        device.last_boot_id = payload.get("boot_id") or device.last_boot_id
        device.last_seen_at = now
        db.add(device)
    else:
        deployment.status = allowed[event_type]

    if deployment.status in TERMINAL_FAILURE_STATUSES or event_type == "failed":
        deployment.last_error = (message or "")[:1000]

    db.add(deployment)
    db.add(
        FirmwareDeploymentEvent(
            deployment_id=deployment.id, event_type=event_type, source="device", payload=payload or {}, message=message,
        )
    )
    db.flush()

    if deployment.status in TERMINAL_STATUSES:
        # A concurrency slot just freed up -- give it to the next queued device.
        rollout = db.get(FirmwareRollout, deployment.rollout_id)
        if rollout is not None and not _maybe_auto_pause(db, rollout):
            _promote_queued(db, rollout)

    return deployment


def _resolve_confirmation(deployment: FirmwareDeployment, payload: dict) -> str:
    """Success is marked ONLY here, from the device's own post-restart
    report -- never by offering/dispatching the update, and never by the
    device merely saying "install started". Requires the exact target
    version, a genuinely NEW boot_id, and an explicit non-rollback report."""
    boot_id = payload.get("boot_id")
    version = payload.get("version")
    rolled_back = bool(payload.get("rolled_back"))
    if not boot_id or not version:
        raise FirmwareServiceError("Confirmarea necesita boot_id si version.")
    if boot_id == deployment.boot_id_before:
        raise FirmwareServiceError("boot_id-ul raportat este identic cu cel dinainte de repornire -- nu a avut loc o repornire reala.")
    deployment.boot_id_after = boot_id
    if rolled_back or version != deployment.target_version:
        return FirmwareDeploymentStatus.rolled_back.value
    return FirmwareDeploymentStatus.succeeded.value


def sweep_expired_deployments(db: Session) -> dict:
    """Called periodically (firmware_deployment_sweep_task): offers that
    were never picked up expire via `offer_expires_at`; devices that
    restarted but never confirmed within the window expire via
    `confirmation_deadline_at` -- issue #168's "Lipsa confirmarii produce
    timed_out"."""
    now = utcnow()
    expired_offers = db.scalars(
        select(FirmwareDeployment).where(
            FirmwareDeployment.status == FirmwareDeploymentStatus.offered.value,
            FirmwareDeployment.offer_expires_at < now,
        )
    ).all()
    expired_confirmations = db.scalars(
        select(FirmwareDeployment).where(
            FirmwareDeployment.status == FirmwareDeploymentStatus.awaiting_confirmation.value,
            FirmwareDeployment.confirmation_deadline_at.is_not(None),
            FirmwareDeployment.confirmation_deadline_at < now,
        )
    ).all()

    touched_rollout_ids: set[uuid.UUID] = set()
    for deployment in (*expired_offers, *expired_confirmations):
        deployment.status = FirmwareDeploymentStatus.timed_out.value
        db.add(deployment)
        db.add(
            FirmwareDeploymentEvent(
                deployment_id=deployment.id, event_type="timed_out", source="system",
                message="Oferta a expirat." if deployment in expired_offers else "Confirmarea post-repornire nu a sosit la timp.",
            )
        )
        touched_rollout_ids.add(deployment.rollout_id)
    if expired_offers or expired_confirmations:
        db.flush()

    for rollout_id in touched_rollout_ids:
        rollout = db.get(FirmwareRollout, rollout_id)
        if rollout is not None and not _maybe_auto_pause(db, rollout):
            _promote_queued(db, rollout)

    # Any active rollout may also have free capacity even without a fresh
    # timeout in this sweep (e.g. a device confirmed succeeded between
    # sweeps and the report_deployment_event call already tried to promote,
    # but a slower concurrent request could have missed it) -- catch up here.
    for rollout in db.scalars(select(FirmwareRollout).where(FirmwareRollout.status == FirmwareRolloutStatus.active.value)).all():
        _promote_queued(db, rollout)

    return {"timed_out": len(expired_offers) + len(expired_confirmations)}
