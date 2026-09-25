from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ControlWindow(StrictModel):
    days: list[int] = Field(min_length=1, max_length=7)
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=1, le=1440)

    @model_validator(mode="after")
    def valid_window(self):
        if any(day not in range(7) for day in self.days) or self.end_minute <= self.start_minute:
            raise ValueError("Fereastra locala invalida; impartiti ferestrele peste miezul noptii.")
        return self


class ControlPolicyIn(StrictModel):
    schema_version: Literal[1] = 1
    device_id: uuid.UUID
    min_soc_percent: Decimal = Field(ge=0, le=100)
    max_soc_percent: Decimal = Field(ge=0, le=100)
    max_managed_energy_kwh: Decimal = Field(ge=0, le=100000)
    max_charge_kw: Decimal = Field(ge=0, le=10000)
    max_discharge_kw: Decimal = Field(ge=0, le=10000)
    max_ramp_kw_per_minute: Decimal = Field(ge=0, le=10000)
    max_efc_day: Decimal = Field(ge=0, le=100)
    max_efc_month: Decimal = Field(ge=0, le=3100)
    windows: list[ControlWindow] = Field(min_length=1, max_length=28)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def valid_band(self):
        if self.min_soc_percent >= self.max_soc_percent:
            raise ValueError("Rezerva trebuie sa fie sub SOC maxim.")
        return self


class ReadbackIn(StrictModel):
    schema_version: Literal[1] = 1
    observed_at: AwareDatetime
    command_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    target_soc_percent: Decimal = Field(ge=0, le=100)
    battery_power_kw: Decimal = Field(ge=-10000, le=10000)
    quality: Literal["measured"] = "measured"


class EVCapabilities(StrictModel):
    start_stop: bool = False
    current_limit: bool = False
    power_limit: bool = False
    meter: bool = False
    phases: Literal[1, 3] | None = None
    vehicle_soc: bool = False
    schedule: bool = False
    v2g: bool = False


class EVSEIn(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    device_id: uuid.UUID | None = None
    max_power_kw: Decimal | None = Field(default=None, ge=0, le=1000, max_digits=8, decimal_places=3)
    capabilities: EVCapabilities = Field(default_factory=EVCapabilities)
    vehicle_data_consent: bool = False
    retention_days: int = Field(default=365, ge=7, le=3650)


class VehicleIn(StrictModel):
    alias: str = Field(min_length=1, max_length=100)
    battery_capacity_kwh: Decimal | None = Field(default=None, gt=0, le=1000, max_digits=8, decimal_places=3)
    consumption_kwh_100km: Decimal | None = Field(default=None, gt=0, le=200, max_digits=8, decimal_places=3)


class EVObservationIn(StrictModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    state: Literal["disconnected", "connected", "available", "charging", "paused", "completed", "faulted"]
    meter_kwh: Decimal | None = Field(default=None, ge=0, le=9999999999, max_digits=16, decimal_places=6)
    power_kw: Decimal | None = Field(default=None, ge=0, le=1000, max_digits=8, decimal_places=3)
    vehicle_soc_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    meter_epoch: str | None = Field(default=None, min_length=1, max_length=64)
    quality: Literal["measured", "estimated", "stale", "simulated"] = "measured"
    vehicle_id: uuid.UUID | None = None


class EVSchedule(StrictModel):
    weekdays: list[int] = Field(min_length=1, max_length=7)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    fold: Literal[0, 1] = 1

    @model_validator(mode="after")
    def valid_days(self):
        if any(day not in range(7) for day in self.weekdays):
            raise ValueError("Zile invalide.")
        return self


class EVRequirementIn(StrictModel):
    minimum_energy_kwh: Decimal = Field(ge=0, le=1000, max_digits=9, decimal_places=3)
    vehicle_id: uuid.UUID | None = None
    target_soc_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    target_range_km: Decimal | None = Field(default=None, ge=0, le=10000, max_digits=8, decimal_places=2)
    deadline: AwareDatetime | None = None
    schedule: EVSchedule | None = None
    max_cost_lei: Decimal | None = Field(default=None, ge=0, le=100000, max_digits=10, decimal_places=4)

    @model_validator(mode="after")
    def deadline_or_schedule(self):
        if (self.deadline is None) == (self.schedule is None):
            raise ValueError("Alegeti o plecare unica sau un program saptamanal.")
        return self

