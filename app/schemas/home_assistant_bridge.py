"""Strict, bounded contract for the HACS HTTPS bridge (independent of MQTT)."""

from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

KINDS = {
    "power": (("sensor",), ("W", "kW")),
    "energy": (("sensor",), ("Wh", "kWh")),
    "temperature": (("sensor",), ("°C", "°F")),
    "occupancy": (("binary_sensor",), ("boolean",)),
    "boiler_status": (("binary_sensor", "sensor"), ("state",)),
    "hvac_status": (("binary_sensor", "sensor"), ("state",)),
    "evse_status": (("binary_sensor", "sensor"), ("state",)),
    "flexibility": (("sensor", "input_number"), ("W", "kW")),
}
Kind = Literal[
    "power",
    "energy",
    "temperature",
    "occupancy",
    "boiler_status",
    "hvac_status",
    "evse_status",
    "flexibility",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported version")
        return value


class PairingRedeem(Contract):
    code: str = Field(min_length=16, max_length=32, repr=False)
    instance_id: UUID
    instance_name: str = Field(min_length=1, max_length=80)


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=160)
    kind: Kind
    unit: str
    device_class: str | None = None
    state_class: str | None = None
    max_age_seconds: int = Field(default=180, ge=30, le=3600)
    source_validated: StrictBool = False
    quality: Literal["estimated", "declared", "simulated"] = "estimated"

    @model_validator(mode="after")
    def eligible(self):
        domains, units = KINDS[self.kind]
        if self.entity_id.split(".")[0] not in domains or self.unit not in units:
            raise ValueError("Entity domain or unit mismatch")
        if self.kind in ("power", "energy", "temperature"):
            if self.device_class != self.kind:
                raise ValueError("Device class mismatch")
            classes = ("total", "total_increasing") if self.kind == "energy" else ("measurement",)
            if self.state_class not in classes:
                raise ValueError("State class mismatch")
        if self.kind in ("power", "energy") and not self.source_validated:
            raise ValueError("Validate the energy source before sharing it")
        if self.kind == "occupancy" and self.device_class not in ("occupancy", "presence"):
            raise ValueError("Only aggregate occupancy sensors are eligible")
        if self.kind == "flexibility" and self.quality not in ("declared", "simulated"):
            raise ValueError("Flexibility is declared, never measured")
        return self


class MappingUpdate(Contract):
    expected_version: int = Field(ge=0)
    consent: Literal[True]
    occupancy_consent: StrictBool = False
    insights_consent: StrictBool = False
    mappings: list[Mapping] = Field(min_length=1, max_length=20)

    @field_validator("consent", mode="before")
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError("Explicit consent required")
        return value

    @model_validator(mode="after")
    def allowlist(self):
        if len({m.entity_id for m in self.mappings}) != len(self.mappings):
            raise ValueError("Duplicate entity")
        count = sum(m.kind == "occupancy" for m in self.mappings)
        if count > 1 or (count and not self.occupancy_consent):
            raise ValueError("Aggregate occupancy requires separate consent")
        return self


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_id: str = Field(max_length=160)
    observed_at: AwareDatetime
    sample_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:+-]+$")
    source: Literal["home_assistant"]
    kind: Kind
    unit: str
    quality: Literal["estimated", "declared", "simulated", "unknown"]
    available: StrictBool
    value: str | StrictBool | None


class Ingest(Contract):
    mapping_version: int = Field(ge=1)
    samples: list[Sample] = Field(max_length=20)


def normalized_value(mapping: Mapping, sample: Sample):
    if (sample.kind, sample.unit) != (mapping.kind, mapping.unit):
        raise ValueError("Mapping mismatch")
    if not sample.available or sample.quality == "unknown":
        if sample.value is not None:
            raise ValueError("Unavailable is null")
        return None
    if sample.quality != mapping.quality:
        raise ValueError("Quality mismatch")
    if mapping.kind == "occupancy":
        if type(sample.value) is not bool:
            raise ValueError("Occupancy must be aggregate boolean")
        return sample.value
    if mapping.kind.endswith("_status"):
        if sample.value not in (
            "on",
            "off",
            "idle",
            "heating",
            "cooling",
            "defrosting",
            "charging",
            "connected",
            "disconnected",
        ):
            raise ValueError("Unknown state")
        return sample.value
    if not isinstance(sample.value, str) or len(sample.value) > 32:
        raise ValueError("Numeric values must be decimal strings")
    try:
        value = Decimal(sample.value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal") from exc
    if (
        not value.is_finite()
        or value.as_tuple().exponent < -6
        or value.copy_abs() > Decimal("1e15")
    ):
        raise ValueError("Invalid precision")
    if mapping.unit in ("W", "Wh"):
        value /= 1000
    elif mapping.unit == "°F":
        value = (value - 32) * Decimal(5) / 9
    low, high = (
        (-100, 100)
        if mapping.kind == "temperature"
        else (0, 1000000000 if mapping.kind == "energy" else 1000)
    )
    if not low <= value <= high:
        raise ValueError("Out of range")
    return str(value)
