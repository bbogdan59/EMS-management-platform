from __future__ import annotations

from datetime import UTC
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    field_validator,
    model_validator,
)

KINDS = {
    "occupancy": ("Ocupare agregata a locuintei", ("binary_sensor",), ("boolean",)),
    "boiler_status": ("Stare boiler", ("binary_sensor", "sensor"), ("state",)),
    "hvac_status": ("Stare HVAC", ("binary_sensor", "sensor"), ("state",)),
    "power": ("Putere consumator", ("sensor",), ("W", "kW")),
    "flexibility": ("Putere flexibila declarata", ("input_number", "sensor"), ("W", "kW")),
}
Kind = Literal["occupancy", "boiler_status", "hvac_status", "power", "flexibility"]
Quality = Literal["measured", "estimated", "declared", "simulated", "stale", "unknown"]


class MappingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=160)
    kind: Kind
    unit: Literal["boolean", "state", "W", "kW"]
    max_age_seconds: int = Field(default=180, ge=30, le=3600)

    @model_validator(mode="after")
    def allowlisted_entity(self):
        _, domains, units = KINDS[self.kind]
        if self.entity_id.split(".")[0] not in domains or self.unit not in units:
            raise ValueError("Entitatea sau unitatea nu este permisa pentru acest tip de senzor.")
        return self


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    revision: int = Field(ge=0)
    broker_key: str = Field(min_length=1, max_length=80)
    username: SecretStr = Field(default=SecretStr(""), max_length=256)
    password: SecretStr = Field(default=SecretStr(""), max_length=1024)
    consent: Literal[True]
    publish_consent: StrictBool = False
    mappings: list[MappingInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_entities(self):
        if len({m.entity_id for m in self.mappings}) != len(self.mappings):
            raise ValueError("O entitate poate fi mapata o singura data.")
        if bool(self.username.get_secret_value()) != bool(self.password.get_secret_value()):
            raise ValueError("Completeaza impreuna utilizatorul si parola MQTT.")
        return self


class ContextSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1]
    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=160)
    sample_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:+-]+$")
    observed_at: AwareDatetime
    source: Literal["home_assistant", "mqtt"]
    quality: Quality
    available: StrictBool
    kind: Kind
    unit: Literal["boolean", "state", "W", "kW"]
    value: Any

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported contract version")
        return value

    @field_validator("observed_at")
    @classmethod
    def utc_instant(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def typed_value(self):
        if self.unit not in KINDS[self.kind][2]:
            raise ValueError("Unit mismatch")
        if not self.available or self.quality == "unknown":
            if self.value is not None:
                raise ValueError("Unavailable/unknown samples must have a null value")
            return self
        if self.value is None:
            raise ValueError("Available samples need a value")
        if self.kind == "occupancy":
            if type(self.value) is not bool:
                raise ValueError("Aggregate occupancy is boolean only")
        elif self.kind.endswith("_status"):
            if self.value not in ("on", "off", "idle", "heating", "cooling", "defrosting"):
                raise ValueError("Unsupported operating state")
        else:
            try:
                if type(self.value) not in (str, int, float, Decimal):
                    raise ValueError("Power must be a decimal number")
                value = Decimal(str(self.value))
                if not value.is_finite() or not 0 <= value <= (
                    1_000_000 if self.unit == "W" else 1000
                ):
                    raise ValueError("Invalid power")
                if value.as_tuple().exponent < -6:
                    raise ValueError("Excessive precision")
                self.value = value
            except InvalidOperation as exc:
                raise ValueError("Invalid power") from exc
            if self.kind == "flexibility" and self.quality not in (
                "declared",
                "simulated",
                "stale",
            ):
                raise ValueError("Flexibility is declared, never a measured capability")
        return self
