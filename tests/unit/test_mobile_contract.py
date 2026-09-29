import copy
import json
from pathlib import Path

import pytest

from scripts.export_mobile_contract import DESTINATION, mobile_contract

BASELINE = Path(__file__).parents[1] / "fixtures/mobile-v1-compatibility.json"


def semantic(value):
    if isinstance(value, list):
        return [semantic(item) for item in value]
    if isinstance(value, dict):
        return {
            key: semantic(item)
            for key, item in value.items()
            if key not in ("title", "description", "default", "examples")
        }
    return value


def signatures(contract):
    result = {}
    for name, schema in contract["components"]["schemas"].items():
        for field, definition in schema.get("properties", {}).items():
            result[f"{name}.{field}"] = {
                "schema": semantic(definition),
                "required": field in schema.get("required", []),
            }
    for path, operations in contract["paths"].items():
        operation = operations["get"]
        result[path] = {
            "id": operation["operationId"],
            "security": operation["security"],
            "parameters": {p["name"]: semantic(p) for p in operation.get("parameters", [])},
            "responses": semantic(operation["responses"]),
        }
    return result


def assert_compatible(baseline, current):
    # Existing response types/nullability and parameter contracts are frozen in v1.
    # New response fields/endpoints and optional query parameters are additive.
    for key, old in baseline.items():
        assert key in current, f"Removed {key}"
        new = current[key]
        if key.startswith("/"):
            assert new["id"] == old["id"] and new["security"] == old["security"], key
            for code, response in old["responses"].items():
                assert new["responses"].get(code) == response, key
            for name, parameter in old["parameters"].items():
                assert new["parameters"].get(name) == parameter, (key, name)
            for name in new["parameters"].keys() - old["parameters"].keys():
                assert not new["parameters"][name].get("required"), (key, name)
        else:
            assert new == old, key


def test_published_contract_matches_application_and_initial_release():
    contract = mobile_contract()
    assert contract == json.loads(DESTINATION.read_text())
    assert_compatible(json.loads(BASELINE.read_text()), signatures(contract))
    assert {
        "/api/v1/mobile/" + path
        for path in (
            "overview",
            "charts/energy",
            "notifications",
            "charging-sessions",
            "home-assistant/context",
        )
    } <= contract["paths"].keys()
    assert not any(path.startswith("/api/v1/mobile/auth/") for path in contract["paths"])
    for operations in contract["paths"].values():
        assert set(operations) == {"get"}
        assert operations["get"]["responses"]["422"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/MobileError")


@pytest.mark.parametrize("mutation", ["nullability", "removed", "required_parameter", "auth"])
def test_compatibility_guard_rejects_breaking_changes(mutation):
    baseline = json.loads(BASELINE.read_text())
    changed = copy.deepcopy(baseline)
    if mutation == "nullability":
        changed["MobileMetric.value"]["schema"] = {"type": "string"}
    elif mutation == "removed":
        del changed["MobileMetric.received_at"]
    elif mutation == "auth":
        changed["/api/v1/mobile/overview"]["security"] = []
    else:
        changed["/api/v1/mobile/overview"]["parameters"]["new"] = {"required": True}
    with pytest.raises(AssertionError):
        assert_compatible(baseline, changed)


def test_compatibility_guard_allows_additive_fields():
    baseline = json.loads(BASELINE.read_text())
    changed = copy.deepcopy(baseline)
    changed["MobileOverview.extra"] = {"schema": {"type": "string"}, "required": False}
    changed["/api/v1/mobile/overview"]["parameters"]["new"] = {"required": False}
    assert_compatible(baseline, changed)
