from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Quality = Literal["measured", "derived", "stale", "simulated", "missing"]


class VoltageSource(BaseModel):
    id: str
    label: str
    provider: str
    last_reported_at: datetime


class VoltageReading(BaseModel):
    value_v: str | None
    measured_at: datetime | None
    received_at: datetime | None
    quality: Quality
    flags: list[str]


class VoltageMinute(BaseModel):
    start: datetime
    mean_v: str | None
    min_v: str | None
    max_v: str | None
    samples: int
    quality: Quality
    flags: list[str]


class VoltagePhase(BaseModel):
    phase: Literal["L1", "L2", "L3"]
    latest: VoltageReading
    min_v: str | None
    max_v: str | None
    samples: int
    observed_minutes: int
    quality: Quality
    flags: list[str]
    points: list[VoltageMinute]


class GridVoltageHistory(BaseModel):
    schema_version: Literal[1] = 1
    station_id: UUID
    timezone: str
    day: date
    today: date
    earliest_day: date
    start: datetime
    end: datetime
    generated_at: datetime
    resolution: Literal["1m"] = "1m"
    aggregation: Literal["sample_mean_with_extrema"] = "sample_mean_with_extrema"
    unit: Literal["V"] = "V"
    sources: list[VoltageSource]
    source_id: str | None
    elapsed_minutes: int
    phases: list[VoltagePhase]
