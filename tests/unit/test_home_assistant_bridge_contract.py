import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.home_assistant_bridge import (
    Ingest,
    Mapping,
    MappingUpdate,
    Sample,
    normalized_value,
)
from scripts.export_home_assistant_bridge_contract import schema


def test_published_schema_matches_server():
    path = Path(__file__).parents[2] / "contracts/home_assistant/bridge-v1.schema.json"
    assert json.loads(path.read_text()) == schema()


@pytest.mark.parametrize("value", [True, "1", 1.0, 2])
def test_versions_are_strict(value):
    with pytest.raises(ValidationError):
        Ingest.model_validate_json(
            json.dumps({"schema_version": value, "mapping_version": 1, "samples": []})
        )


@pytest.mark.parametrize(
    ("kind", "unit", "value", "expected"),
    [
        ("power", "W", "1250", "1.25"),
        ("energy", "Wh", "0", "0"),
        ("temperature", "°F", "32", "0"),
        ("occupancy", "boolean", False, False),
    ],
)
def test_decimal_and_false_normalization(kind, unit, value, expected):
    mapping = Mapping(
        entity_id="binary_sensor.home" if kind == "occupancy" else "sensor.house",
        kind=kind,
        unit=unit,
        device_class=kind,
        state_class="total" if kind == "energy" else "measurement",
        source_validated=True,
    )
    sample = Sample.model_validate_json(
        json.dumps(
            {
                "entity_id": mapping.entity_id,
                "kind": kind,
                "unit": unit,
                "source": "home_assistant",
                "quality": "estimated",
                "available": True,
                "value": value,
                "observed_at": "2026-09-29T00:00:00Z",
                "sample_id": "test",
            }
        )
    )
    assert normalized_value(mapping, sample) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999999999", "-1", "0.0000001"])
def test_invalid_numeric_payload_is_rejected_without_overflow(value):
    mapping = Mapping(
        entity_id="sensor.house",
        kind="power",
        unit="W",
        device_class="power",
        state_class="measurement",
        source_validated=True,
    )
    sample = Sample.model_validate_json(
        json.dumps(
            {
                "entity_id": "sensor.house",
                "kind": "power",
                "unit": "W",
                "source": "home_assistant",
                "quality": "estimated",
                "available": True,
                "value": value,
                "observed_at": "2026-09-29T00:00:00Z",
                "sample_id": "test",
            }
        )
    )
    with pytest.raises(ValueError):
        normalized_value(mapping, sample)


def test_numeric_consent_cannot_opt_in():
    with pytest.raises(ValidationError):
        MappingUpdate.model_validate_json(
            json.dumps({"expected_version": 1, "consent": 1, "mappings": []})
        )
