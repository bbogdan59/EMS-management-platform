from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Quality = Literal["measured", "derived", "simulated", "stale", "missing"]


class Metric(BaseModel):
    value: str | None
    unit: Literal["V", "A", "W"]
    supported: bool
    quality: Quality


class SolarInput(BaseModel):
    id: UUID
    index: int
    label: str
    kind: Literal["mppt", "pv_input"]
    source: str
    capability: Literal["reported"]
    metrics: dict[str, Metric]
    flags: list[str]
    measured_at: str | None
    received_at: str | None
    freshness: Literal["missing", "stale", "fresh"]
    revision: int
    configuration: dict


class ACObservation(BaseModel):
    value: str | None = None
    unit: Literal["W"] = "W"
    measured_at: str | None = None
    received_at: str | None = None
    flags: list[str]
    quality: Quality


class SolarInverterSnapshot(BaseModel):
    id: UUID
    label: str
    model: str | None
    source: str
    inputs: list[SolarInput]
    dc_total_w: str | None
    dc_quality: Quality
    ac_output: ACObservation
    warnings: list[dict[str, str]]


class SolarSnapshot(BaseModel):
    schema_version: Literal[1]
    station_id: UUID
    timezone: str
    generated_at: str
    inverters: list[SolarInverterSnapshot]
    dc_ac_note: str


class SolarHistoryPoint(BaseModel):
    start: str
    end: str
    voltage_v: str | None
    current_a: str | None
    power_w: str | None
    energy_kwh: str | None
    coverage: dict[str, str]
    quality: Quality
    flags: list[str]


class SolarHistory(BaseModel):
    schema_version: Literal[1]
    tracker_id: UUID
    timezone: str
    resolution: Literal["15m", "1h"]
    start: str
    end: str
    units: dict[str, str]
    aggregation: Literal["time_weighted_mean"]
    points: list[SolarHistoryPoint]


class PVString(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    identifier: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str = Field(min_length=1, max_length=80)
    modules: int | None = Field(None, ge=1, le=10000)


class TrackerConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    revision: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=80)
    comparison_group: str | None = Field(None, max_length=64)
    installed_kw: Decimal | None = Field(None, gt=0, le=100000)
    azimuth_deg: Decimal | None = Field(None, ge=0, lt=360)
    tilt_deg: Decimal | None = Field(None, ge=0, le=90)
    warning_threshold_percent: Decimal = Field(Decimal(30), ge=10, le=90)
    strings: list[PVString] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def comparison_ready(self):
        if self.comparison_group and any(
            v is None for v in (self.installed_kw, self.azimuth_deg, self.tilt_deg)
        ):
            raise ValueError("Completeaza puterea, azimutul si inclinarea pentru comparatie.")
        if len({s.identifier for s in self.strings}) != len(self.strings):
            raise ValueError("Identificatorii sirurilor trebuie sa fie unici.")
        return self
