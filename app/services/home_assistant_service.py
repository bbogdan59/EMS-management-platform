from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import delete, select, update

from app.config import get_settings
from app.core.audit import record_audit
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.security import utcnow
from app.models.control import Recommendation, StationControl
from app.models.home_assistant import HomeAssistantConnection, HomeAssistantMapping
from app.models.station import Station
from app.schemas.home_assistant import ConnectionInput, ContextSample

STATUS_LABELS = {
    "pending": "Configurat · conexiune neverificata",
    "test_pending": "Test in asteptarea workerului",
    "connected": "Conexiune MQTT verificata",
    "retrying": "Reconectare programata",
    "revoked": "Deconectat · consimtamant retras",
}
ERROR_LABELS = {
    "auth_failed": "Brokerul a refuzat autentificarea. Verifica sau roteste credentialele.",
    "access_denied": "Brokerul a refuzat accesul la topicurile statiei. Verifica ACL-ul.",
    "tls_failed": "Certificatul TLS nu a putut fi verificat.",
    "unavailable": "Brokerul nu raspunde. Workerul va reincerca automat.",
    "credentials_unreadable": "Credentialele trebuie introduse din nou dupa schimbarea cheii de criptare.",
    "broker_disabled": "Brokerul nu mai este permis in configuratia platformei.",
    "message_limit": "Prea multe mesaje. Verifica publicarea doar a senzorilor selectati.",
}


def broker_endpoint(key):
    settings = get_settings()
    endpoint = (
        settings.home_assistant_mqtt_brokers.get(key)
        if settings.home_assistant_mqtt_enabled
        else None
    )
    try:
        parsed = urlsplit(endpoint or "")
        if (
            parsed.scheme != "mqtts"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return parsed.hostname, parsed.port or 8883
    except ValueError:
        raise ValueError("Broker MQTT/TLS indisponibil in configuratia platformei.") from None


def get_connection(db, station_id, *, lock=False):
    query = select(HomeAssistantConnection).where(HomeAssistantConnection.station_id == station_id)
    if lock:
        query = query.with_for_update()
    return db.scalar(query.execution_options(populate_existing=True))


def mappings_for(db, connection):
    if connection is None:
        return []
    return db.scalars(
        select(HomeAssistantMapping)
        .where(HomeAssistantMapping.connection_id == connection.id)
        .order_by(HomeAssistantMapping.entity_id)
    ).all()


def topic_root(connection):
    return f"ems/v1/{connection.station_id}/{connection.stream_id}"


def context_topic(connection, mapping):
    return f"{topic_root(connection)}/context/{mapping.id}"


def _audit(db, station, user, action, connection):
    record_audit(
        db,
        action=f"home_assistant.{action}",
        resource_type="home_assistant_connection",
        resource_id=str(connection.id),
        station_id=station.id,
        actor_user_id=user.id,
        actor_label="user",
        metadata={"revision": connection.revision, "publish_enabled": connection.publish_enabled},
    )


def configure(db, station, user, data: ConnectionInput):
    broker_endpoint(data.broker_key)
    db.scalar(select(Station).where(Station.id == station.id).with_for_update(key_share=True))
    connection = get_connection(db, station.id, lock=True)
    if data.revision != (connection.revision if connection else 0):
        raise ValueError("Configuratia a fost modificata. Reincarca pagina inainte de salvare.")
    username, password = data.username.get_secret_value(), data.password.get_secret_value()
    if not username and (
        not connection
        or not connection.encrypted_credentials
        or connection.broker_key != data.broker_key
    ):
        raise ValueError("Introdu credentiale MQTT dedicate acestei statii.")
    if connection is None:
        connection = HomeAssistantConnection(station_id=station.id, revision=1)
        db.add(connection)
    else:
        connection.revision += 1
        db.execute(
            delete(HomeAssistantMapping).where(HomeAssistantMapping.connection_id == connection.id)
        )
    if username:
        connection.encrypted_credentials = encrypt_secret(
            json.dumps({"username": username, "password": password})
        )
    connection.broker_key = data.broker_key
    connection.enabled, connection.publish_enabled = True, data.publish_consent
    connection.consent_at, connection.consent_by = utcnow(), user.id
    connection.stream_id = uuid.uuid4()
    connection.status, connection.error_code = "pending", None
    connection.failure_count, connection.rejected_messages = 0, 0
    connection.last_attempt_at = connection.last_connected_at = connection.last_published_at = None
    connection.next_attempt_at = utcnow()
    db.flush()
    for mapping in data.mappings:
        db.add(HomeAssistantMapping(connection_id=connection.id, **mapping.model_dump()))
    _audit(db, station, user, "configured", connection)
    db.flush()
    return connection


def manage(db, station, user, action, revision, *, username=None, password=None):
    connection = get_connection(db, station.id, lock=True)
    if not connection or connection.revision != revision:
        raise ValueError("Configuratia a fost modificata sau nu exista. Reincarca pagina.")
    if action == "revoke":
        connection.enabled = connection.publish_enabled = False
        connection.encrypted_credentials = connection.consent_at = connection.consent_by = None
        connection.status, connection.error_code = "revoked", None
        connection.revision += 1
        connection.stream_id = uuid.uuid4()
        connection.next_attempt_at = None
        db.execute(
            delete(HomeAssistantMapping).where(HomeAssistantMapping.connection_id == connection.id)
        )
    elif action in ("test", "rotate") and connection.enabled:
        broker_endpoint(connection.broker_key)
        if action == "rotate":
            if not username or not password or len(username) > 256 or len(password) > 1024:
                raise ValueError("Introdu utilizatorul si parola MQTT noi.")
            connection.encrypted_credentials = encrypt_secret(
                json.dumps({"username": username, "password": password})
            )
            connection.revision += 1
        connection.status, connection.error_code = "test_pending", None
        connection.next_attempt_at, connection.failure_count = utcnow(), 0
    else:
        raise ValueError("Integrarea trebuie activata inainte de testare.")
    _audit(db, station, user, action, connection)
    db.flush()


def ingest(connection, mappings, topic, payload: bytes, *, now=None):
    now = now or utcnow()
    if not connection.enabled or not connection.consent_at:
        return "disabled"
    mapping = next((m for m in mappings if context_topic(connection, m) == topic), None)
    if mapping is None or len(payload) > 4096:
        return "rejected"
    try:
        sample = ContextSample.model_validate_json(payload)
    except (ValidationError, ValueError):
        return "rejected"
    if (sample.entity_id, sample.kind, sample.unit) != (
        mapping.entity_id,
        mapping.kind,
        mapping.unit,
    ) or sample.observed_at > now + timedelta(seconds=30):
        return "rejected"
    if sample.sample_id == mapping.sample_id or (
        mapping.observed_at and sample.observed_at <= mapping.observed_at
    ):
        return "duplicate_or_older"
    # Replayed retained values do not gain freshness just because they arrived again.
    if sample.observed_at < now - timedelta(hours=24):
        return "expired"
    value = sample.value
    if isinstance(value, Decimal):
        value = str(value / 1000 if mapping.unit == "W" else value)
    mapping.value, mapping.observed_at = value, sample.observed_at
    mapping.sample_id, mapping.received_at = sample.sample_id, now
    mapping.quality, mapping.available, mapping.source = (
        sample.quality,
        sample.available,
        sample.source,
    )
    return "accepted"


def observation(mapping, *, enabled=True, now=None):
    now = now or utcnow()
    age = (now - mapping.observed_at).total_seconds() if mapping.observed_at else None
    stale = age is not None and age > mapping.max_age_seconds
    available = bool(
        enabled
        and mapping.available
        and mapping.observed_at
        and not stale
        and mapping.quality not in ("unknown", "stale")
    )
    return {
        "entity_id": mapping.entity_id,
        "kind": mapping.kind,
        "value": mapping.value if available else None,
        "unit": "kW" if mapping.kind in ("power", "flexibility") else mapping.unit,
        "available": available,
        "quality": "stale" if stale else mapping.quality,
        "source_quality": mapping.quality,
        "is_simulated": mapping.quality == "simulated",
        "is_stale": stale or mapping.quality == "stale",
        "source": mapping.source,
        "observed_at": mapping.observed_at,
        "received_at": mapping.received_at,
        "age_seconds": max(0, int(age)) if age is not None else None,
        "usable_as_measured": available and mapping.quality == "measured",
        "control_authorized": False,
    }


def purge_context(db, *, now=None):
    now = now or utcnow()
    # Keep the timestamp watermark for replay protection, but erase expired values.
    return db.execute(
        update(HomeAssistantMapping)
        .where(
            HomeAssistantMapping.observed_at < now - timedelta(hours=24),
        )
        .values(value=None, available=False, quality="stale", source=None, sample_id=None)
    ).rowcount


def publication(db, connection, *, now=None):
    now = now or utcnow()
    station = db.get(Station, connection.station_id)
    control = db.scalar(
        select(StationControl).where(StationControl.station_id == connection.station_id)
    )
    rows = db.scalars(
        select(Recommendation)
        .where(
            Recommendation.station_id == connection.station_id,
            Recommendation.status == "available",
            Recommendation.expires_at > now,
        )
        .order_by(Recommendation.created_at.desc())
        .limit(5)
    ).all()
    return {
        "schema_version": 1,
        "station_id": str(connection.station_id),
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "source": "ems",
        "control_mode": control.mode if control else None,
        "state_provenance": "configured",
        "is_simulated": station.is_demo,
        "capabilities": {
            "context_import": True,
            "recommendations": True,
            "physical_control": False,
            "commands": [],
        },
        "recommendations": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "status": r.status,
                "title": str(r.snapshot.get("title", "Recomandare EMS"))[:160],
                "confidence": r.snapshot.get("confidence"),
                "coverage": r.snapshot.get("coverage"),
                "quality": "recommendation",
                "reason_code": r.snapshot.get("reason_code"),
                "expires_at": r.expires_at.isoformat(),
                "review_path": f"/stations/{connection.station_id}/recommendations/{r.id}",
            }
            for r in rows
        ],
    }


def credentials(connection):
    from app.core.crypto import DecryptionError

    try:
        result = json.loads(decrypt_secret(connection.encrypted_credentials))
        if (
            not isinstance(result, dict)
            or set(result) != {"username", "password"}
            or not all(isinstance(v, str) and v for v in result.values())
        ):
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise DecryptionError("Credentiale MQTT indisponibile.") from None


def next_attempt(failures, now: datetime, jitter_seconds=0):
    return now + timedelta(seconds=min(900, 15 * 2 ** min(failures - 1, 6)) + jitter_seconds)
