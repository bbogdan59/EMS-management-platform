import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.home_assistant import HomeAssistantConnection, HomeAssistantMapping
from app.schemas.home_assistant import ContextSample, MappingInput
from app.services import home_assistant_service as service
from app.services.home_assistant_config import package_yaml

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def sample(**overrides):
    return {
        "schema_version": 1,
        "entity_id": "sensor.boiler_power",
        "kind": "power",
        "unit": "W",
        "sample_id": "sample-1",
        "observed_at": NOW.isoformat(),
        "source": "home_assistant",
        "quality": "measured",
        "available": True,
        "value": "1234.567",
        **overrides,
    }


def context():
    connection = HomeAssistantConnection(
        id=uuid4(),
        station_id=uuid4(),
        stream_id=uuid4(),
        enabled=True,
        consent_at=NOW,
        publish_enabled=True,
    )
    mapping = HomeAssistantMapping(
        id=uuid4(),
        connection_id=connection.id,
        entity_id="sensor.boiler_power",
        kind="power",
        unit="W",
        max_age_seconds=180,
        quality="unknown",
        available=False,
    )
    return connection, mapping


@pytest.mark.parametrize("value", [0, "0", 0.0, "1234.567"])
def test_power_zero_and_decimal_are_not_coerced_to_boolean(value):
    parsed = ContextSample(**sample(value=value))
    assert parsed.value == Decimal(str(value))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"observed_at": "2026-09-28T12:00:00"},
        {"value": "NaN"},
        {"value": "Infinity"},
        {"value": "-1"},
        {"value": "1000001"},
        {"value": True},
        {"value": {}},
        {"value": "0.0000001"},
        {"attributes": {"person": "private"}},
        {"command": "turn_on"},
        {"available": False},
        {"available": "false"},
        {"quality": "good"},
        {"kind": "occupancy", "unit": "boolean", "value": 1},
        {"kind": "flexibility", "quality": "measured"},
        {"unit": "kWh"},
    ],
)
def test_contract_rejects_unsafe_or_ambiguous_payloads(changes):
    with pytest.raises(ValidationError):
        ContextSample(**sample(**changes))


@pytest.mark.parametrize(
    "entity,kind,unit",
    [
        ("person.bogdan", "occupancy", "boolean"),
        ("device_tracker.phone", "occupancy", "boolean"),
        ("climate.bedroom", "hvac_status", "state"),
        ("sensor.power", "occupancy", "boolean"),
        ("sensor.power", "power", "kWh"),
        ("sensor.+", "power", "W"),
    ],
)
def test_mapping_is_explicit_and_minimised(entity, kind, unit):
    with pytest.raises(ValidationError):
        MappingInput(entity_id=entity, kind=kind, unit=unit)


def test_utc_offset_is_preserved_as_an_instant_across_dst_and_leap_day():
    first = ContextSample(**sample(observed_at="2024-10-27T03:30:00+03:00"))
    second = ContextSample(**sample(observed_at="2024-10-27T03:30:00+02:00"))
    assert second.observed_at - first.observed_at == timedelta(hours=1)
    assert ContextSample(**sample(observed_at="2024-02-29T00:00:00+02:00")).observed_at == datetime(
        2024, 2, 28, 22, tzinfo=UTC
    )


def test_duplicates_out_of_order_and_retained_staleness_keep_original_provenance():
    connection, mapping = context()
    topic = service.context_topic(connection, mapping)
    raw = json.dumps(sample(quality="simulated")).encode()
    assert service.ingest(connection, [mapping], topic, raw, now=NOW) == "accepted"
    assert mapping.value == "1.234567"
    assert (
        service.ingest(connection, [mapping], topic, raw, now=NOW + timedelta(minutes=5))
        == "duplicate_or_older"
    )
    assert mapping.received_at == NOW
    old = json.dumps(
        sample(sample_id="old", observed_at=(NOW - timedelta(seconds=1)).isoformat(), value="0")
    ).encode()
    assert service.ingest(connection, [mapping], topic, old, now=NOW) == "duplicate_or_older"
    view = service.observation(mapping, now=NOW + timedelta(minutes=5))
    assert view["value"] is None and view["is_stale"] and view["is_simulated"]
    assert view["source_quality"] == "simulated" and view["quality"] == "stale"
    assert not view["control_authorized"] and not view["usable_as_measured"]


def test_unknown_offline_and_real_zero_remain_distinct():
    connection, mapping = context()
    topic = service.context_topic(connection, mapping)
    assert service.observation(mapping, now=NOW)["value"] is None
    service.ingest(connection, [mapping], topic, json.dumps(sample(value=0)).encode(), now=NOW)
    assert service.observation(mapping, now=NOW)["value"] == "0"
    unavailable = sample(
        sample_id="offline",
        observed_at=(NOW + timedelta(seconds=1)).isoformat(),
        available=False,
        value=None,
    )
    service.ingest(
        connection,
        [mapping],
        topic,
        json.dumps(unavailable).encode(),
        now=NOW + timedelta(seconds=1),
    )
    assert service.observation(mapping, now=NOW + timedelta(seconds=1))["value"] is None


@pytest.mark.parametrize("change", ["topic", "identity", "future", "oversized", "old", "disabled"])
def test_forged_topics_payloads_or_expired_consent_do_not_import(change):
    connection, mapping = context()
    topic = service.context_topic(connection, mapping)
    values = sample()
    if change == "topic":
        topic = "ems/v1/another-station/context/anything"
    elif change == "identity":
        values["entity_id"] = "sensor.another"
    elif change == "future":
        values["observed_at"] = (NOW + timedelta(minutes=2)).isoformat()
    elif change == "old":
        values["observed_at"] = (NOW - timedelta(days=2)).isoformat()
    elif change == "disabled":
        connection.enabled = False
    payload = json.dumps(values).encode() if change != "oversized" else b" " * 4097
    assert service.ingest(connection, [mapping], topic, payload, now=NOW) != "accepted"
    assert mapping.value is None


def test_generated_package_contains_only_selected_entities_and_read_only_actions():
    connection, mapping = context()
    package = json.loads("\n".join(package_yaml(connection, [mapping]).splitlines()[2:]))
    automation = package["automation"][0]
    assert automation["variables"]["entity"] == mapping.entity_id
    assert "last_reported" in automation["variables"]["observed"]
    assert len(automation["actions"]) == 1
    assert automation["actions"][0]["action"] == "mqtt.publish"
    assert automation["actions"][0]["data"]["topic"] == service.context_topic(connection, mapping)
    assert "attributes" not in automation["actions"][0]["data"]["payload"]
    assert package["mqtt"]["sensor"][0]["expire_after"] == 120
    connection.publish_enabled = False
    assert '"mqtt":' not in package_yaml(connection, [mapping])


def test_backoff_is_bounded_and_persistent_timestamp_compatible():
    assert service.next_attempt(1, NOW) == NOW + timedelta(seconds=15)
    assert service.next_attempt(2, NOW) == NOW + timedelta(seconds=30)
    assert service.next_attempt(1000, NOW, 5) == NOW + timedelta(seconds=905)
