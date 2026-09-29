from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.core.crypto import DecryptionError
from app.core.security import utcnow
from app.models.home_assistant import HomeAssistantConnection
from app.models.organization import Organization
from app.models.station import Station
from app.services import home_assistant_mqtt as transport
from app.services import home_assistant_service as service


def poll_connection(db, connection_id, *, now=None):
    settings = get_settings()
    if not settings.home_assistant_mqtt_enabled:
        return {"skipped": "disabled"}
    now = now or utcnow()
    # A row lock fences configuration/revocation and concurrent worker deliveries.
    # It is held only for the bounded network session, never across task retries.
    connection = db.scalar(
        select(HomeAssistantConnection)
        .where(
            HomeAssistantConnection.id == connection_id,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if not connection or not connection.enabled or not connection.consent_at:
        return {"skipped": "disabled_or_busy"}
    if connection.next_attempt_at and connection.next_attempt_at > now:
        return {"skipped": "backoff"}
    station = db.get(Station, connection.station_id)
    organization = db.get(Organization, station.organization_id)
    if not station.is_active or organization.status == "archived":
        return {"skipped": "station_inactive"}
    connection.last_attempt_at = now
    mappings = service.mappings_for(db, connection)
    try:
        try:
            endpoint = service.broker_endpoint(connection.broker_key)
        except ValueError:
            raise transport.MQTTFailure("broker_disabled") from None
        outbound = (
            (
                service.topic_root(connection) + "/ems/state",
                service.publication(db, connection, now=now),
            )
            if connection.publish_enabled
            else None
        )
        messages = transport.exchange(
            endpoint=endpoint,
            credentials=service.credentials(connection),
            topics=[service.context_topic(connection, m) for m in mappings],
            client_id="ems-" + connection.id.hex,
            publication=outbound,
            ca_file=settings.home_assistant_mqtt_ca_file,
        )
        accepted = rejected = 0
        for topic, payload in messages:
            outcome = service.ingest(connection, mappings, topic, payload, now=now)
            accepted += outcome == "accepted"
            rejected += outcome == "rejected"
        connection.status, connection.error_code = "connected", None
        connection.failure_count = 0
        connection.rejected_messages += rejected
        connection.last_connected_at = now
        if outbound:
            connection.last_published_at = now
        connection.next_attempt_at = now + timedelta(seconds=30)
        db.flush()
        return {"status": "connected", "accepted": accepted, "rejected": rejected}
    except (transport.MQTTFailure, DecryptionError) as exc:
        code = exc.code if isinstance(exc, transport.MQTTFailure) else "credentials_unreadable"
        connection.failure_count += 1
        connection.error_code, connection.status = code, "retrying"
        connection.next_attempt_at = service.next_attempt(
            connection.failure_count, now, secrets.randbelow(6)
        )
        db.flush()
        return {"status": "retrying", "error_code": code}
