"""Contract canonic, provider-agnostic, pentru consumatori termici flexibili.

Aceste modele descriu capabilitati si scenarii de simulare. Ele NU reprezinta
o autorizatie de control si nu sunt expuse in API-ul dispozitivului inca.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ThermalMode(StrEnum):
    off = "off"
    heat = "heat"
    cool = "cool"
    auto = "auto"
    dhw = "dhw"


class AutomationStage(StrEnum):
    recommendation = "recommendation"
    shadow = "shadow"
    live = "live"


class FlexibleLoadCapabilities(BaseModel):
    """Capabilitati raportate si verificate separat, nu presupuse din model."""

    model_config = ConfigDict(extra="forbid")

    modes: set[ThermalMode] = Field(min_length=1)
    rated_input_power_kw: Decimal = Field(gt=0)
    minimum_setpoint_c: Decimal
    maximum_setpoint_c: Decimal
    setpoint_step_c: Decimal = Field(gt=0)
    minimum_on_minutes: int = Field(default=10, ge=0)
    minimum_off_minutes: int = Field(default=10, ge=0)
    reports_indoor_temperature: bool = False
    reports_power: bool = False
    reports_operating_state: bool = False
    supports_local_failsafe: bool = False

    @model_validator(mode="after")
    def validate_setpoint_range(self):
        if self.minimum_setpoint_c >= self.maximum_setpoint_c:
            raise ValueError("minimum_setpoint_c trebuie sa fie sub maximum_setpoint_c")
        return self


class ComfortWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    minimum_temperature_c: Decimal
    maximum_temperature_c: Decimal
    preferred_temperature_c: Decimal | None = None
    quiet_hours: bool = False

    @model_validator(mode="after")
    def validate_window(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("intervalele de confort trebuie sa includa fusul orar")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at trebuie sa fie dupa starts_at")
        if self.minimum_temperature_c > self.maximum_temperature_c:
            raise ValueError("temperatura minima nu poate depasi maxima")
        if self.preferred_temperature_c is not None and not (
            self.minimum_temperature_c
            <= self.preferred_temperature_c
            <= self.maximum_temperature_c
        ):
            raise ValueError("temperatura preferata trebuie sa fie in banda de confort")
        return self


class FlexibleLoadPolicy(BaseModel):
    """Preferinte explicite ale clientului; `live` nu este implicit niciodata."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    maximum_automation_stage: AutomationStage = AutomationStage.recommendation
    comfort_windows: list[ComfortWindow] = Field(default_factory=list)
    maximum_input_power_kw: Decimal | None = Field(default=None, gt=0)
    override_until: datetime | None = None
    opt_out_reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.override_until is not None and self.override_until.tzinfo is None:
            raise ValueError("override_until trebuie sa includa fusul orar")
        ordered = sorted(self.comfort_windows, key=lambda window: window.starts_at)
        if any(left.ends_at > right.starts_at for left, right in pairwise(ordered)):
            raise ValueError("intervalele de confort nu se pot suprapune")
        return self


class ThermalModelParameters(BaseModel):
    """Model RC de ordinul intai, calibrabil per cladire."""

    model_config = ConfigDict(extra="forbid")

    time_constant_hours: Decimal = Field(gt=0)
    thermal_capacity_kwh_per_c: Decimal = Field(gt=0)
    coefficient_of_performance: Decimal = Field(gt=0)


class ThermalSimulationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    outdoor_temperature_c: Decimal
    requested_mode: ThermalMode
    requested_input_power_kw: Decimal = Field(ge=0)
    # Preturile dinamice pot fi negative; `None` ramane distinct de zero.
    import_price_lei_kwh: Decimal | None = None

    @model_validator(mode="after")
    def validate_step(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("pasii de simulare trebuie sa includa fusul orar")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at trebuie sa fie dupa starts_at")
        if self.requested_mode == ThermalMode.off and self.requested_input_power_kw != 0:
            raise ValueError("modul off necesita putere zero")
        return self


class ThermalSimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_indoor_temperature_c: Decimal
    capabilities: FlexibleLoadCapabilities
    model: ThermalModelParameters
    steps: list[ThermalSimulationStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_schedule(self):
        for previous, current in pairwise(self.steps):
            if previous.ends_at != current.starts_at:
                raise ValueError("pasii de simulare trebuie sa fie contigui")
        for step in self.steps:
            if step.requested_mode not in self.capabilities.modes:
                raise ValueError(f"modul {step.requested_mode} nu este raportat ca suportat")
            if step.requested_input_power_kw > self.capabilities.rated_input_power_kw:
                raise ValueError("puterea ceruta depaseste puterea nominala raportata")
        return self


class ThermalSimulationResultStep(BaseModel):
    starts_at: datetime
    ends_at: datetime
    indoor_temperature_start_c: Decimal
    indoor_temperature_end_c: Decimal
    input_energy_kwh: Decimal
    cost_lei: Decimal | None
    runtime_violation: str | None = None


class ThermalSimulationResult(BaseModel):
    steps: list[ThermalSimulationResultStep]
    total_input_energy_kwh: Decimal
    total_cost_lei: Decimal | None
    has_unknown_cost: bool
