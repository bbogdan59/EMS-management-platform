"""Pairing and ingestion serialize with revoke on the station bridge row."""

import base64
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.audit import record_audit
from app.core.security import hash_token, utcnow
from app.models.home_assistant_bridge import HomeAssistantBridge
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import User
from app.schemas.home_assistant_bridge import Mapping, normalized_value


def get_bridge(db, station_id, *, lock=False):
    query = select(HomeAssistantBridge).where(HomeAssistantBridge.station_id == station_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)


def audit(db, station, user, action, bridge):
    record_audit(
        db,
        action=f"home_assistant_bridge.{action}",
        resource_type="home_assistant_bridge",
        resource_id=str(bridge.id),
        station_id=station.id,
        actor_user_id=user.id,
        actor_label="user",
        organization_id=station.organization_id,
        metadata={"mapping_version": bridge.mapping_version},
    )


def audit_bridge(db, bridge, action):
    record_audit(
        db,
        action=f"home_assistant_bridge.{action}",
        resource_type="home_assistant_bridge",
        resource_id=str(bridge.id),
        actor_label="home_assistant_bridge",
        organization_id=bridge.organization_id,
        station_id=bridge.station_id,
        metadata={"mapping_version": bridge.mapping_version},
    )


def revoke(bridge):
    bridge.pairing_hash = bridge.pairing_expires_at = None
    bridge.token_hash = bridge.token_expires_at = None
    bridge.instance_id = bridge.instance_name = None
    bridge.enabled = bridge.occupancy_consent = bridge.insights_consent = False
    bridge.mappings, bridge.observations = [], {}
    bridge.last_seen_at = None
    bridge.mapping_version += 1


def issue_pairing(db, station, user):
    # The station lock also fences concurrent first-time creation (unique station_id).
    db.scalar(select(Station).where(Station.id == station.id).with_for_update())
    bridge = get_bridge(db, station.id, lock=True)
    if bridge is None:
        bridge = HomeAssistantBridge(
            station_id=station.id, organization_id=station.organization_id, mapping_version=0
        )
        db.add(bridge)
    revoke(bridge)
    bridge.organization_id, bridge.consent_by = station.organization_id, user.id
    code = base64.b32encode(secrets.token_bytes(12)).decode().rstrip("=")
    bridge.pairing_hash = hash_token(code)
    bridge.pairing_expires_at = utcnow() + timedelta(minutes=10)
    db.flush()
    audit(db, station, user, "pairing_created", bridge)
    return bridge, code


def owner_active(db, bridge):
    station = db.get(Station, bridge.station_id)
    org = db.get(Organization, bridge.organization_id)
    user = db.get(User, bridge.consent_by)
    if not station or not station.is_active or station.organization_id != bridge.organization_id:
        return False
    if not org or org.status != "active" or not user or not user.is_active:
        return False
    return (
        user.is_platform_admin
        or db.scalar(
            select(Membership.id).where(
                Membership.organization_id == bridge.organization_id,
                Membership.user_id == user.id,
                Membership.is_active.is_(True),
                Membership.role == "organization_admin",
            )
        )
        is not None
    )


def redeem(db, data):
    bridge = db.scalar(
        select(HomeAssistantBridge)
        .where(HomeAssistantBridge.pairing_hash == hash_token(data.code.upper().replace("-", "")))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not bridge
        or not bridge.pairing_expires_at
        or bridge.pairing_expires_at <= utcnow()
        or not owner_active(db, bridge)
    ):
        raise ValueError("invalid_pairing")
    token = secrets.token_urlsafe(32)
    bridge.pairing_hash = bridge.pairing_expires_at = None
    bridge.token_hash = hash_token(token)
    bridge.token_expires_at = utcnow() + timedelta(minutes=10)
    bridge.instance_id, bridge.instance_name = data.instance_id, data.instance_name
    db.flush()
    audit_bridge(db, bridge, "pairing_redeemed")
    return bridge, token


def authenticate(db, token):
    if not 32 <= len(token) <= 128:
        return None
    bridge = db.scalar(
        select(HomeAssistantBridge)
        .where(HomeAssistantBridge.token_hash == hash_token(token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not bridge
        or not bridge.token_expires_at
        or bridge.token_expires_at <= utcnow()
        or not owner_active(db, bridge)
    ):
        return None
    return bridge


def configure(db, bridge, data):
    mappings = [m.model_dump() for m in data.mappings]
    same = (
        bridge.mappings == mappings
        and bridge.occupancy_consent == data.occupancy_consent
        and bridge.insights_consent == data.insights_consent
        and bridge.enabled
    )
    # A lost response can be retried without creating a new mapping generation.
    if data.expected_version + 1 == bridge.mapping_version and same:
        return
    if data.expected_version != bridge.mapping_version:
        raise ValueError("mapping_conflict")
    bridge.mapping_version += 1
    bridge.mappings, bridge.observations = mappings, {}
    bridge.occupancy_consent, bridge.insights_consent = (
        data.occupancy_consent,
        data.insights_consent,
    )
    bridge.enabled = True
    bridge.token_expires_at = utcnow() + timedelta(days=90)
    audit_bridge(db, bridge, "configured")


def ingest(bridge, data):
    if not bridge.enabled or data.mapping_version != bridge.mapping_version:
        raise ValueError("mapping_conflict")
    now = utcnow()
    allowed = {m["entity_id"]: Mapping.model_validate(m) for m in bridge.mappings}
    observations = dict(bridge.observations)
    accepted = ignored = 0
    for sample in data.samples:
        mapping = allowed.get(sample.entity_id)
        if mapping is None or sample.observed_at > now + timedelta(seconds=30):
            raise ValueError("invalid_sample")
        value = normalized_value(mapping, sample)
        old = observations.get(sample.entity_id)
        if sample.observed_at < now - timedelta(hours=24) or (
            old
            and (
                old["sample_id"] == sample.sample_id
                or sample.observed_at <= datetime.fromisoformat(old["observed_at"])
            )
        ):
            ignored += 1
            continue
        observations[sample.entity_id] = {
            **sample.model_dump(mode="json"),
            "value": value,
            "unit": {"W": "kW", "Wh": "kWh", "°F": "°C"}.get(mapping.unit, mapping.unit),
            "received_at": now.isoformat(),
        }
        accepted += 1
    bridge.observations, bridge.last_seen_at = observations, now
    return {"accepted": accepted, "ignored": ignored, "mapping_version": bridge.mapping_version}


def snapshot(bridge, *, enabled=True, now=None):
    now = now or utcnow()
    state = "disconnected"
    observations = []
    if bridge and enabled:
        if bridge.pairing_hash and bridge.pairing_expires_at > now:
            state = "connecting"
        elif bridge.token_hash:
            if bridge.token_expires_at <= now:
                state = "reauth_required"
            elif not bridge.enabled or not bridge.last_seen_at:
                state = "connecting"
            else:
                age = (now - bridge.last_seen_at).total_seconds()
                state = "offline" if age > 300 else "stale" if age > 90 else "connected"
        for mapping in bridge.mappings:
            sample = bridge.observations.get(mapping["entity_id"])
            age = (
                (now - datetime.fromisoformat(sample["observed_at"])).total_seconds()
                if sample
                else None
            )
            stale = age is None or age > mapping["max_age_seconds"]
            available = bool(
                sample
                and sample["available"]
                and not stale
                and state == "connected"
                and sample["quality"] != "unknown"
            )
            observations.append(
                {
                    **mapping,
                    **(sample or {}),
                    "available": available,
                    "value": sample["value"] if available else None,
                    "is_stale": stale,
                    "source": "home_assistant",
                    "source_quality": mapping["quality"],
                    "is_simulated": mapping["quality"] == "simulated",
                    "quality": "stale" if stale else sample["quality"],
                }
            )
        if state == "connected" and observations and any(not o["available"] for o in observations):
            state = "stale"
    return {
        "schema_version": 1,
        "status": state,
        "enabled": bool(enabled and bridge and bridge.enabled and bridge.token_hash),
        "mapping_version": bridge.mapping_version if bridge else 0,
        "instance_name": bridge.instance_name if bridge else None,
        "last_seen_at": bridge.last_seen_at if bridge else None,
        "observations": observations,
        "physical_control": False,
        "insights_consent": bool(bridge and bridge.insights_consent),
    }


def purge_context(db):
    cutoff = utcnow() - timedelta(hours=24)
    # Keep replay watermarks, erase only values. Row locks fence ingestion/revocation.
    for bridge in db.scalars(select(HomeAssistantBridge).with_for_update(skip_locked=True)):
        values = dict(bridge.observations)
        changed = False
        for key, value in values.items():
            if (
                value.get("value") is not None
                and datetime.fromisoformat(value["observed_at"]) < cutoff
            ):
                values[key] = {**value, "value": None, "available": False}
                changed = True
        if changed:
            bridge.observations = values
