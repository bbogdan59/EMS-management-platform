"""Publish additive solar read/configuration and device-extension schemas."""

import argparse
import json
from pathlib import Path

from app.schemas.device_api import TelemetryItem
from app.schemas.solar import SolarHistory, SolarSnapshot, TrackerConfiguration

ROOT = Path(__file__).resolve().parents[1] / "contracts/solar"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, model in (
        ("snapshot-v1", SolarSnapshot),
        ("history-v1", SolarHistory),
        ("configuration-v1", TrackerConfiguration),
        ("telemetry-v1", TelemetryItem),
    ):
        path = ROOT / f"{name}.schema.json"
        content = (
            json.dumps(model.model_json_schema(mode="validation"), indent=2, ensure_ascii=False)
            + "\n"
        )
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Stale solar contract: {path.name}")
        else:
            path.write_text(content)


if __name__ == "__main__":
    main()
