"""Tests for the OTA firmware service (issue #168): release registry,
rollout state machine, device-facing offer/confirm flow, idempotency,
compatibility/downgrade gating, concurrency, auto-pause, timeout sweep."""
from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import get_settings
from app.core.security import utcnow
from app.services import firmware_service, firmware_storage
from tests.factories import make_device, make_org, make_station, make_user


class _FakeStorage(firmware_storage.ReleaseStorage):
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put(self, key, data, *, content_type="application/octet-stream"):
        self.data[key] = data

    def generate_download_url(self, key, *, ttl_seconds):
        return f"fake://{key}?ttl={ttl_seconds}"

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture()
def storage():
    return _FakeStorage()


@pytest.fixture()
def signing_key(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(get_settings(), "firmware_signing_private_key_pem", pem)
    monkeypatch.setattr(get_settings(), "firmware_signing_key_id", "test-key-1")
    return key


_counter = [0]


def _admin(db):
    _counter[0] += 1
    return make_user(db, email=f"fwadmin{_counter[0]}@test.local", is_platform_admin=True)


def _compatible_device(db, station, *, firmware_version="1.0.0", hardware_platform="raspberry-pi-4", architecture="arm64", name="Test Device"):
    device = make_device(db, station, name=name)
    device.firmware_version = firmware_version
    device.hardware_platform = hardware_platform
    device.architecture = architecture
    db.add(device)
    db.flush()
    return device


def _release(db, user, storage, *, version="1.1.0", **overrides):
    kwargs = {
        "version": version, "channel": "stable", "hardware_platform": "raspberry-pi-4", "architecture": "arm64",
        "protocol_schema_version": 1, "artifact_bytes": b"fake-artifact-bytes",
    }
    kwargs.update(overrides)
    return firmware_service.create_release(db, user, storage=storage, **kwargs)


def _published_release(db, user, storage, **overrides):
    release = _release(db, user, storage, **overrides)
    return firmware_service.publish_release(db, release)


# --- Release registry ------------------------------------------------------


def test_create_release_stores_artifact_hash_and_signature(db, signing_key, storage):
    user = _admin(db)
    release = _release(db, user, storage)
    assert release.status == "draft"
    assert release.artifact_size_bytes == len(b"fake-artifact-bytes")
    assert len(release.sha256_hex) == 64
    assert len(release.signature_ed25519_hex) == 128
    signing_key.public_key().verify(bytes.fromhex(release.signature_ed25519_hex), b"fake-artifact-bytes")
    assert storage.data[release.storage_key] == b"fake-artifact-bytes"


def test_create_release_requires_signing_key_configured(db, storage):
    user = _admin(db)
    with pytest.raises(firmware_service.FirmwareServiceError, match="nu este configurata"):
        _release(db, user, storage)


def test_create_release_rejects_duplicate_version(db, signing_key, storage):
    user = _admin(db)
    _release(db, user, storage, version="9.9.9")
    with pytest.raises(firmware_service.FirmwareServiceError, match="Exista deja"):
        _release(db, user, storage, version="9.9.9")


def test_create_release_rejects_empty_artifact(db, signing_key, storage):
    user = _admin(db)
    with pytest.raises(firmware_service.FirmwareServiceError, match="gol"):
        _release(db, user, storage, artifact_bytes=b"")


def test_create_release_rejects_oversized_artifact(db, signing_key, storage, monkeypatch):
    monkeypatch.setattr(get_settings(), "firmware_max_artifact_bytes", 4)
    user = _admin(db)
    with pytest.raises(firmware_service.FirmwareServiceError, match="limita"):
        _release(db, user, storage, artifact_bytes=b"way too big")


def test_publish_release_sets_published_at(db, signing_key, storage):
    user = _admin(db)
    release = _release(db, user, storage)
    published = firmware_service.publish_release(db, release)
    assert published.status == "published"
    assert published.published_at is not None


def test_publish_release_rejects_non_draft(db, signing_key, storage):
    user = _admin(db)
    release = _published_release(db, user, storage)
    with pytest.raises(firmware_service.FirmwareServiceError, match="publicat"):
        firmware_service.publish_release(db, release)


def test_revoke_release_requires_reason(db, signing_key, storage):
    user = _admin(db)
    release = _published_release(db, user, storage)
    with pytest.raises(firmware_service.FirmwareServiceError, match="obligatoriu"):
        firmware_service.revoke_release(db, release, reason="")


def test_revoke_release_cancels_unstarted_deployments_only(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Revoke Org")
    station = make_station(db, org, user, name="Firmware Revoke Station")
    device = _compatible_device(db, station)
    db.commit()

    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    assert deployment.status == "offered"

    firmware_service.revoke_release(db, release, reason="Security issue")
    db.refresh(deployment)
    assert deployment.status == "cancelled"
    assert release.status == "revoked"


# --- Rollout eligibility: compatibility, downgrade ------------------------


def test_create_rollout_requires_published_release(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Draft Org")
    station = make_station(db, org, user, name="Firmware Draft Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _release(db, user, storage)  # still draft
    with pytest.raises(firmware_service.FirmwareServiceError, match="publicat"):
        firmware_service.create_rollout(db, user, release, [device])


def test_create_rollout_skips_hardware_platform_mismatch(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware HW Org")
    station = make_station(db, org, user, name="Firmware HW Station")
    device = _compatible_device(db, station, hardware_platform="raspberry-pi-3")
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    assert result["created"] == []
    assert result["skipped"][str(device.id)] == "hardware_platform_mismatch"


def test_create_rollout_skips_architecture_mismatch(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Arch Org")
    station = make_station(db, org, user, name="Firmware Arch Station")
    device = _compatible_device(db, station, architecture="armhf")
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    assert result["skipped"][str(device.id)] == "architecture_mismatch"


def test_create_rollout_skips_unknown_agent_version_when_min_required(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware MinVer Org")
    station = make_station(db, org, user, name="Firmware MinVer Station")
    device = _compatible_device(db, station, firmware_version=None)
    db.commit()
    release = _published_release(db, user, storage, min_compatible_agent_version="1.0.0")
    result = firmware_service.create_rollout(db, user, release, [device])
    assert result["skipped"][str(device.id)] == "unknown_agent_version"


def test_create_rollout_skips_agent_version_too_old(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware OldVer Org")
    station = make_station(db, org, user, name="Firmware OldVer Station")
    device = _compatible_device(db, station, firmware_version="0.5.0")
    db.commit()
    release = _published_release(db, user, storage, min_compatible_agent_version="1.0.0")
    result = firmware_service.create_rollout(db, user, release, [device])
    assert result["skipped"][str(device.id)] == "agent_version_too_old"


def test_create_rollout_blocks_downgrade_without_allow_flag(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Downgrade Org")
    station = make_station(db, org, user, name="Firmware Downgrade Station")
    device = _compatible_device(db, station, firmware_version="2.0.0")
    db.commit()
    release = _published_release(db, user, storage, version="1.5.0")
    result = firmware_service.create_rollout(db, user, release, [device])
    assert result["skipped"][str(device.id)] == "downgrade_blocked"


def test_create_rollout_allows_downgrade_with_reason(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Downgrade Allow Org")
    station = make_station(db, org, user, name="Firmware Downgrade Allow Station")
    device = _compatible_device(db, station, firmware_version="2.0.0")
    db.commit()
    release = _published_release(db, user, storage, version="1.5.0")
    result = firmware_service.create_rollout(
        db, user, release, [device], allow_downgrade=True, downgrade_reason="Rolling back a bad release"
    )
    assert len(result["created"]) == 1
    assert result["created"][0].is_downgrade is True


def test_create_rollout_allow_downgrade_requires_reason(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Downgrade NoReason Org")
    station = make_station(db, org, user, name="Firmware Downgrade NoReason Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    with pytest.raises(firmware_service.FirmwareServiceError, match="motiv"):
        firmware_service.create_rollout(db, user, release, [device], allow_downgrade=True, downgrade_reason="")


# --- Concurrency: max_concurrent gating + promotion -----------------------


def test_create_rollout_respects_max_concurrent(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Concurrency Org")
    station = make_station(db, org, user, name="Firmware Concurrency Station")
    devices = [_compatible_device(db, station, name=f"D{i}") for i in range(3)]
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, devices, max_concurrent=1)
    statuses = sorted(d.status for d in result["created"])
    assert statuses == ["offered", "requested", "requested"]


def test_promotion_advances_queued_deployment_when_slot_frees(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Promote Org")
    station = make_station(db, org, user, name="Firmware Promote Station")
    devices = [_compatible_device(db, station, name=f"D{i}") for i in range(2)]
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, devices, max_concurrent=1)
    offered = next(d for d in result["created"] if d.status == "offered")
    queued = next(d for d in result["created"] if d.status == "requested")

    firmware_service.report_deployment_event(db, offered.device, offered.id, "rejected", payload={}, message=None)
    db.refresh(queued)
    assert queued.status == "offered"


# --- request_deployment idempotency ----------------------------------------


def test_request_deployment_is_idempotent_while_non_terminal(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Idempotent Org")
    station = make_station(db, org, user, name="Firmware Idempotent Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    first = result["created"][0]

    again = firmware_service.request_deployment(db, user, result["rollout"], release, device, is_downgrade=False)
    assert again.id == first.id
    assert again.attempt == 1


def test_request_deployment_creates_new_attempt_after_failure(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Retry Org")
    station = make_station(db, org, user, name="Firmware Retry Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    first = result["created"][0]
    firmware_service.report_deployment_event(db, device, first.id, "downloading", payload={}, message=None)
    firmware_service.report_deployment_event(db, device, first.id, "failed", payload={}, message="disk full")
    db.refresh(first)
    assert first.status == "failed"

    retried = firmware_service.request_deployment(db, user, result["rollout"], release, device, is_downgrade=False)
    assert retried.id != first.id
    assert retried.attempt == 2
    assert retried.status == "requested"


# --- Device-facing offer + full state machine ------------------------------


def test_get_current_offer_returns_none_when_nothing_pending(db):
    org = make_org(db, "Firmware NoOffer Org")
    station = make_station(db, org, make_user(db, is_platform_admin=True), name="Firmware NoOffer Station")
    device = _compatible_device(db, station)
    db.commit()
    assert firmware_service.get_current_offer(db, device) is None


def test_get_current_offer_expires_stale_offer(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Expire Org")
    station = make_station(db, org, user, name="Firmware Expire Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    deployment.offer_expires_at = utcnow() - timedelta(minutes=1)
    db.add(deployment)
    db.flush()

    offer = firmware_service.get_current_offer(db, device)
    assert offer is None
    db.refresh(deployment)
    assert deployment.status == "timed_out"


def test_offer_payload_includes_signed_download_url(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Payload Org")
    station = make_station(db, org, user, name="Firmware Payload Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]

    payload = firmware_service.offer_payload(db, deployment, storage)
    assert payload["sha256_hex"] == release.sha256_hex
    assert payload["signature_ed25519_hex"] == release.signature_ed25519_hex
    assert release.storage_key in payload["download_url"]


def _drive_to_awaiting_confirmation(db, deployment, device, *, boot_id_before="boot-before"):
    firmware_service.report_deployment_event(db, device, deployment.id, "downloading", payload={}, message=None)
    firmware_service.report_deployment_event(db, device, deployment.id, "verified", payload={}, message=None)
    firmware_service.report_deployment_event(db, device, deployment.id, "installing", payload={}, message=None)
    firmware_service.report_deployment_event(
        db, device, deployment.id, "restarting", payload={"boot_id": boot_id_before}, message=None
    )
    db.refresh(deployment)
    return deployment


def test_full_happy_path_reaches_succeeded_and_updates_device(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Happy Org")
    station = make_station(db, org, user, name="Firmware Happy Station")
    device = _compatible_device(db, station, firmware_version="1.0.0")
    db.commit()
    release = _published_release(db, user, storage, version="1.1.0")
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    assert deployment.status == "offered"

    _drive_to_awaiting_confirmation(db, deployment, device)
    assert deployment.status == "awaiting_confirmation"
    assert deployment.boot_id_before == "boot-before"
    assert deployment.confirmation_deadline_at is not None

    confirmed = firmware_service.report_deployment_event(
        db, device, deployment.id, "confirmed",
        payload={"boot_id": "boot-after", "version": "1.1.0", "rolled_back": False}, message=None,
    )
    assert confirmed.status == "succeeded"
    assert confirmed.boot_id_after == "boot-after"
    db.refresh(device)
    assert device.firmware_version == "1.1.0"
    assert device.firmware_version_source == "ota_confirmation"
    assert device.last_boot_id == "boot-after"


def test_confirmation_with_unchanged_version_is_rolled_back(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Rollback Org")
    station = make_station(db, org, user, name="Firmware Rollback Station")
    device = _compatible_device(db, station, firmware_version="1.0.0")
    db.commit()
    release = _published_release(db, user, storage, version="1.1.0")
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    _drive_to_awaiting_confirmation(db, deployment, device)

    resolved = firmware_service.report_deployment_event(
        db, device, deployment.id, "confirmed",
        payload={"boot_id": "boot-after", "version": "1.0.0", "rolled_back": True}, message="install failed self-check",
    )
    assert resolved.status == "rolled_back"
    db.refresh(device)
    assert device.firmware_version == "1.0.0"


def test_confirmation_requires_a_genuinely_new_boot_id(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware SameBoot Org")
    station = make_station(db, org, user, name="Firmware SameBoot Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    _drive_to_awaiting_confirmation(db, deployment, device, boot_id_before="same-boot")

    with pytest.raises(firmware_service.FirmwareServiceError, match="repornire reala"):
        firmware_service.report_deployment_event(
            db, device, deployment.id, "confirmed",
            payload={"boot_id": "same-boot", "version": release.version}, message=None,
        )


def test_report_event_rejects_invalid_transition(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware BadTransition Org")
    station = make_station(db, org, user, name="Firmware BadTransition Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]

    with pytest.raises(firmware_service.FirmwareServiceError, match="nu este permisa"):
        firmware_service.report_deployment_event(db, device, deployment.id, "installing", payload={}, message=None)


def test_report_event_rejects_wrong_device(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware WrongDevice Org")
    station = make_station(db, org, user, name="Firmware WrongDevice Station")
    device = _compatible_device(db, station)
    other_device = _compatible_device(db, station, name="Other Device")
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]

    with pytest.raises(firmware_service.FirmwareServiceError, match="nu exista pentru acest dispozitiv"):
        firmware_service.report_deployment_event(db, other_device, deployment.id, "downloading", payload={}, message=None)


def test_idempotent_retry_of_terminal_event_is_a_silent_success(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware TerminalRetry Org")
    station = make_station(db, org, user, name="Firmware TerminalRetry Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    firmware_service.report_deployment_event(db, device, deployment.id, "downloading", payload={}, message=None)
    firmware_service.report_deployment_event(db, device, deployment.id, "failed", payload={}, message="disk full")

    same = firmware_service.report_deployment_event(db, device, deployment.id, "failed", payload={}, message="disk full")
    assert same.status == "failed"


def test_contradictory_retry_of_terminal_event_is_rejected(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Contradict Org")
    station = make_station(db, org, user, name="Firmware Contradict Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    firmware_service.report_deployment_event(db, device, deployment.id, "downloading", payload={}, message=None)
    firmware_service.report_deployment_event(db, device, deployment.id, "failed", payload={}, message="disk full")

    with pytest.raises(firmware_service.FirmwareServiceError, match="contradictoriu"):
        firmware_service.report_deployment_event(db, device, deployment.id, "failed", payload={}, message="different reason")


# --- Rollout lifecycle: pause/resume/cancel/auto-pause ---------------------


def test_pause_and_resume_rollout(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware PauseResume Org")
    station = make_station(db, org, user, name="Firmware PauseResume Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    rollout = result["rollout"]

    firmware_service.pause_rollout(db, rollout)
    assert rollout.status == "paused"
    with pytest.raises(firmware_service.FirmwareServiceError, match="pus pe pauza"):
        firmware_service.pause_rollout(db, rollout)

    firmware_service.resume_rollout(db, rollout)
    assert rollout.status == "active"
    with pytest.raises(firmware_service.FirmwareServiceError, match="reluat"):
        firmware_service.resume_rollout(db, rollout)
    assert rollout.status == "active"


def test_cancel_rollout_cancels_unstarted_but_not_mid_install(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Cancel Org")
    station = make_station(db, org, user, name="Firmware Cancel Station")
    devices = [_compatible_device(db, station, name=f"D{i}") for i in range(2)]
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, devices, max_concurrent=2)
    started, offered = result["created"]
    _drive_to_awaiting_confirmation(db, started, started.device)

    firmware_service.cancel_rollout(db, result["rollout"], reason="Bad release detected")
    db.refresh(started)
    db.refresh(offered)
    assert started.status == "awaiting_confirmation"  # not force-cancelled mid-install
    assert offered.status == "cancelled"


def test_auto_pause_on_failure_threshold(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware AutoPause Org")
    station = make_station(db, org, user, name="Firmware AutoPause Station")
    devices = [_compatible_device(db, station, name=f"D{i}") for i in range(2)]
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, devices, max_concurrent=2, failure_threshold_percent=40)
    first, second = result["created"]

    firmware_service.report_deployment_event(db, first.device, first.id, "downloading", payload={}, message=None)
    firmware_service.report_deployment_event(db, first.device, first.id, "failed", payload={}, message="boom")

    db.refresh(result["rollout"])
    assert result["rollout"].status == "paused"
    assert result["rollout"].auto_paused_at is not None


# --- Timeout sweep -----------------------------------------------------


def test_sweep_expires_stale_offers(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware Sweep Org")
    station = make_station(db, org, user, name="Firmware Sweep Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    deployment.offer_expires_at = utcnow() - timedelta(minutes=1)
    db.add(deployment)
    db.flush()

    summary = firmware_service.sweep_expired_deployments(db)
    assert summary["timed_out"] == 1
    db.refresh(deployment)
    assert deployment.status == "timed_out"


def test_sweep_expires_unconfirmed_restart(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware SweepConfirm Org")
    station = make_station(db, org, user, name="Firmware SweepConfirm Station")
    device = _compatible_device(db, station)
    db.commit()
    release = _published_release(db, user, storage)
    result = firmware_service.create_rollout(db, user, release, [device])
    deployment = result["created"][0]
    _drive_to_awaiting_confirmation(db, deployment, device)
    deployment.confirmation_deadline_at = utcnow() - timedelta(minutes=1)
    db.add(deployment)
    db.flush()

    summary = firmware_service.sweep_expired_deployments(db)
    assert summary["timed_out"] == 1
    db.refresh(deployment)
    assert deployment.status == "timed_out"


def test_sweep_promotes_queued_after_a_slot_frees_via_timeout(db, signing_key, storage):
    user = _admin(db)
    org = make_org(db, "Firmware SweepPromote Org")
    station = make_station(db, org, user, name="Firmware SweepPromote Station")
    devices = [_compatible_device(db, station, name=f"D{i}") for i in range(2)]
    db.commit()
    release = _published_release(db, user, storage)
    # High failure_threshold_percent so this single timeout doesn't also
    # trip auto-pause -- that's covered separately by test_auto_pause_on_failure_threshold.
    result = firmware_service.create_rollout(db, user, release, devices, max_concurrent=1, failure_threshold_percent=100)
    offered = next(d for d in result["created"] if d.status == "offered")
    queued = next(d for d in result["created"] if d.status == "requested")
    offered.offer_expires_at = utcnow() - timedelta(minutes=1)
    db.add(offered)
    db.flush()

    firmware_service.sweep_expired_deployments(db)
    db.refresh(queued)
    assert queued.status == "offered"


# --- Version compatibility helpers -----------------------------------------


@pytest.mark.parametrize(("current", "target", "expected"), [
    ("1.0.0", "1.1.0", False),
    ("1.10.0", "1.9.0", True),
    ("2.0.0", "1.9.9", True),
    (None, "1.0.0", False),
])
def test_is_downgrade_heuristic(current, target, expected):
    assert firmware_service.is_downgrade(current, target) is expected
