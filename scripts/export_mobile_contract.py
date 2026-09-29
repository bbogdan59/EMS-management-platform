"""Export only the versioned mobile read API and its reachable schemas."""

import argparse
import json
from pathlib import Path

DESTINATION = Path(__file__).resolve().parents[1] / "contracts/mobile/mobile-v1.openapi.json"


def mobile_contract(document=None):
    if document is None:
        from app.main import app

        document = app.openapi()
    paths = {
        path: operations
        for path, operations in document["paths"].items()
        if any(
            "mobile-v1" in operation.get("tags", [])
            for operation in operations.values()
            if isinstance(operation, dict)
        )
    }
    schemas = {}

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in schemas:
                    schemas[name] = document["components"]["schemas"][name]
                    collect(schemas[name])
            for item in value.values():
                collect(item)

    collect(paths)
    security_names = {
        name
        for operations in paths.values()
        for operation in operations.values()
        for security in operation.get("security", [])
        for name in security
    }
    return {
        "openapi": document["openapi"],
        "info": {
            "title": "EMS mobile read API",
            "version": "1.0.0",
            "description": "Station-scoped human-session reads. Decimal values are strings; missing values remain null. See contracts/mobile/README.md.",
        },
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                name: document["components"]["securitySchemes"][name] for name in security_names
            },
        },
    }


def encoded_contract():
    return json.dumps(mobile_contract(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    encoded = encoded_contract()
    if args.check:
        if not DESTINATION.exists() or DESTINATION.read_text() != encoded:
            raise SystemExit("Mobile contract drift: run python -m scripts.export_mobile_contract")
    else:
        DESTINATION.parent.mkdir(parents=True, exist_ok=True)
        DESTINATION.write_text(encoded)
