from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Quality = Literal["measured", "estimated", "simulated", "stale", "declared", "missing"]


class BatteryMetric(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    value: Decimal | None = None
    unit: str
    supported: bool | None = None
    quality: Quality = "missing"
    flags: list[str] = Field(default_factory=list)
    source: str | None = None
    measured_at: datetime | None = None
    received_at: datetime | None = None
    method: str | None = None


class BatterySoh(BatteryMetric):
    status: Literal["measured", "estimated", "unavailable"] = "unavailable"
    confidence: Literal["low", "medium", "high", "unknown"] = "unknown"
    method_version: str | None = None


class BatteryTarget(BaseModel):
    id: str
    device_id: uuid.UUID
    pack_id: str | None = None
    label: str
    source: str
    scope: Literal["reported_bank", "pack"]


class BatteryCurrent(BaseModel):
    target: BatteryTarget
    state: Literal["charging", "discharging", "idle", "fault", "unknown"]
    freshness: Literal["fresh", "stale", "missing"]
    soc: BatteryMetric
    power: BatteryMetric
    voltage: BatteryMetric
    current: BatteryMetric
    temperature: BatteryMetric
    nominal_capacity: BatteryMetric
    usable_capacity: BatteryMetric
    stored_energy: BatteryMetric
    reported_cycles: BatteryMetric
    soh: BatterySoh


class BatterySummary(BaseModel):
    schema_version: Literal[1] = 1
    station_id: uuid.UUID
    timezone: str
    generated_at: datetime
    batteries: list[BatteryCurrent]


class BatteryBucket(BaseModel):
    start: datetime
    end: datetime
    charge_kwh: Decimal | None
    discharge_kwh: Decimal | None
    soc_percent: Decimal | None
    temperature_min_c: Decimal | None
    temperature_max_c: Decimal | None
    temperature_min_at: datetime | None
    temperature_max_at: datetime | None
    efc: Decimal | None
    coverage: dict[str, Decimal]
    quality: dict[str, Quality]
    flags: dict[str, list[str]]
    capacity_basis: list[str]


class BatteryPeriod(BaseModel):
    start: datetime
    end: datetime
    charge_kwh: Decimal | None
    discharge_kwh: Decimal | None
    efc: Decimal | None
    efc_quality: Quality
    coverage: Decimal
    quality: Quality
    flags: list[str]
    capacity_basis: list[str]
    efc_reason: Literal["available", "partial_history", "capacity_missing", "no_power_data"]
    complete_days: int


class BatteryExtreme(BaseModel):
    kind: Literal["coldest", "warmest"]
    value_c: Decimal
    measured_at: datetime
    quality: Quality


class BatteryNotice(BaseModel):
    code: str
    severity: str
    title: str
    explanation: str
    measured_at: datetime | None = None
    alert_id: uuid.UUID | None = None


class BatteryDetail(BaseModel):
    schema_version: Literal[1] = 1
    station_id: uuid.UUID
    timezone: str
    generated_at: datetime
    targets: list[BatteryTarget]
    current: BatteryCurrent | None
    day: date
    end_date: date
    days: int
    today: BatteryPeriod
    period: BatteryPeriod
    hourly: list[BatteryBucket]
    daily: list[BatteryBucket]
    temperature_days: list[BatteryBucket]
    extremes: list[BatteryExtreme]
    notices: list[BatteryNotice]
    history_basis: str = "retained_source_telemetry"
    efc_method: str = "dc-throughput-over-two-nominal-capacity/v1"
    max_hold_seconds: int = 300
