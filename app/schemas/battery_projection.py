from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.battery import BatteryMetric, BatteryTarget


class ChargeEstimate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    status: Literal[
        "estimated",
        "already_at_target",
        "not_charging",
        "not_reached",
        "partial",
        "unavailable",
        "beyond_horizon",
    ] = "unavailable"
    reaches_target_at: datetime | None = None
    minutes_to_target: Decimal | None = None
    end_soc_percent: Decimal | None = None
    peak_soc_percent: Decimal | None = None
    reason: str | None = None


class ChargeProjectionPoint(BaseModel):
    at: datetime
    soc_percent: Decimal | None
    pv_kw: Decimal | None = None
    load_kw: Decimal | None = None
    battery_power_kw: Decimal | None = None
    kind: Literal["observed", "estimated", "missing"]


class BatteryChargeProjection(BaseModel):
    schema_version: Literal[1] = 1
    method: Literal["solar-surplus-current-blend/v1"] = "solar-surplus-current-blend/v1"
    station_id: UUID
    timezone: str
    generated_at: datetime
    horizon_end: datetime
    target: BatteryTarget | None = None
    soc: BatteryMetric = Field(default_factory=lambda: BatteryMetric(unit="%"))
    power: BatteryMetric = Field(default_factory=lambda: BatteryMetric(unit="kW"))
    capacity: BatteryMetric = Field(default_factory=lambda: BatteryMetric(unit="kWh"))
    target_soc_percent: Decimal = Decimal(100)
    reserve_soc_percent: Decimal = Decimal(0)
    energy_to_target_kwh: Decimal | None = None
    current_rate: ChargeEstimate = Field(default_factory=ChargeEstimate)
    solar: ChargeEstimate = Field(default_factory=ChargeEstimate)
    points: list[ChargeProjectionPoint] = Field(default_factory=list)
    quality: Literal["estimated", "unavailable"] = "unavailable"
    confidence: Literal["nominal", "low", "unknown"] = "unknown"
    flags: list[str] = Field(default_factory=list)
    forecast_issued_at: datetime | None = None
    consumption_issued_at: datetime | None = None
    config_version: int | None = None
    preference_version: int | None = None
