"""Export the canonical, versioned HACS bridge request schema."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from app.schemas.home_assistant_bridge import Ingest, MappingUpdate, PairingRedeem


def schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "EMS Home Assistant HTTPS bridge requests v1",
        **TypeAdapter(PairingRedeem | MappingUpdate | Ingest).json_schema(),
    }


if __name__ == "__main__":
    destination = (
        Path(__file__).resolve().parents[1] / "contracts/home_assistant/bridge-v1.schema.json"
    )
    destination.write_text(json.dumps(schema(), indent=2, ensure_ascii=False) + "\n")
