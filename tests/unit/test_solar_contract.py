import json

from app.schemas.device_api import TelemetryItem
from app.schemas.solar import SolarHistory, SolarSnapshot, TrackerConfiguration
from scripts.export_solar_contract import ROOT


def test_published_solar_contract_matches_authoritative_schemas():
    for name, model in (
        ("snapshot-v1", SolarSnapshot),
        ("history-v1", SolarHistory),
        ("configuration-v1", TrackerConfiguration),
        ("telemetry-v1", TelemetryItem),
    ):
        assert json.loads((ROOT / f"{name}.schema.json").read_text()) == model.model_json_schema(
            mode="validation"
        )
