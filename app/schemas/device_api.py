"""Scheme Pydantic pentru API-ul v1 destinat dispozitivului (viitorul
controler local). Documentate integral in docs/API.md, cu exemple JSON."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClaimRequest(BaseModel):
    claim_code: str = Field(..., description="Codul afisat de operator in UI, ex: EMS-7F3K-9QRT")
    device_name: str = Field(..., max_length=200)
    hardware_info: dict = Field(default_factory=dict, description="Informatii libere despre hardware (model, serie).")


class ClaimResponse(BaseModel):
    device_id: uuid.UUID
    station_id: uuid.UUID
    credential_secret: str = Field(..., description="Secret afisat O SINGURA DATA. Stocheaza-l local, nu poate fi recuperat.")


class HeartbeatRequest(BaseModel):
    boot_id: str = Field(..., description="Identificator unic per pornire a dispozitivului, pentru deduplicare telemetrie.")
    firmware_version: str | None = None
    capabilities: dict = Field(
        default_factory=dict,
        description=(
            "Capabilitati RAPORTATE de dispozitiv (ex. poate_limita_incarcare, "
            "putere_max_incarcare_w). Nu implica automat o capabilitate de comanda "
            "-- o limita de putere raportata nu devine o capabilitate presupusa."
        ),
    )


class HeartbeatResponse(BaseModel):
    server_time: datetime
    device_status: str
    has_active_plan: bool
    pending_command_count: int


class RotateCredentialResponse(BaseModel):
    device_id: uuid.UUID
    credential_secret: str


class TelemetryItem(BaseModel):
    """Un punct de telemetrie. Conventii de semn documentate in docs/API.md:
    battery_power_w>0 inseamna incarcare; grid_power_w>0 inseamna import."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    boot_id: str
    sequence: int = Field(..., ge=0, description="Contor monoton crescator in cadrul unui boot_id, pentru deduplicare.")
    schema_version: int = Field(default=1, ge=1)
    measured_at: datetime

    pv_power_w: Decimal | None = Field(default=None, description="W, >=0. Respingere semantica per item daca este negativa.")
    load_power_w: Decimal | None = Field(default=None, description="W, >=0. Respingere semantica per item daca este negativa.")
    battery_power_w: Decimal | None = Field(default=None, description="W; >0 incarcare, <0 descarcare.")
    grid_power_w: Decimal | None = Field(default=None, description="W; >0 import, <0 export.")
    battery_soc_percent: Decimal | None = Field(default=None, description="%, interval valid 0..100; 0 este valoare masurata valida.")
    ev_connected: bool | None = None
    ev_power_w: Decimal | None = Field(default=None, description="W, >=0. Respingere semantica per item daca este negativa.")

    quality_flags: dict = Field(default_factory=dict)
    raw_payload: dict = Field(default_factory=dict)

    @field_validator("measured_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("measured_at trebuie sa includa fusul orar (ex. sufix Z sau +02:00).")
        return v


class TelemetryBatchRequest(BaseModel):
    items: list[TelemetryItem] = Field(..., min_length=1)


class TelemetryItemAck(BaseModel):
    """Rezultat stabil pentru un item, in aceeasi ordine ca request-ul."""

    boot_id: str
    sequence: int
    status: Literal["accepted", "duplicate", "rejected"]
    retryable: bool = False
    reason_code: str | None = None


class TelemetryBatchResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    errors: list[str] = Field(default_factory=list)
    # Camp aditiv: clientii v1 care citesc doar contoarele raman compatibili.
    results: list[TelemetryItemAck] = Field(default_factory=list)


class PlanIntervalOut(BaseModel):
    interval_start: datetime
    interval_end: datetime
    battery_power_target_kw: Decimal
    grid_power_target_kw: Decimal
    battery_soc_target_percent: Decimal
    ev_charge_power_kw: Decimal
    explanation: str | None = None


class ActivePlanResponse(BaseModel):
    plan_id: uuid.UUID | None
    version: int | None
    status: str | None
    execution_mode: str | None = Field(
        default=None,
        description="shadow = doar informativ, NU trebuie executat fizic; live = poate fi executat, sub validarea locala a dispozitivului.",
    )
    published_at: datetime | None
    intervals: list[PlanIntervalOut] = Field(default_factory=list)


class StationConfigOut(BaseModel):
    station_id: uuid.UUID
    timezone: str
    execution_mode: str
    inverter_power_kw: Decimal
    battery_max_charge_power_kw: Decimal | None
    battery_max_discharge_power_kw: Decimal | None
    grid_import_limit_kw: Decimal | None
    grid_export_limit_kw: Decimal | None
    allow_grid_charge: bool
    allow_battery_export: bool
    min_reserve_soc_percent: Decimal
    max_normal_soc_percent: Decimal
    config_version: int
    preference_version: int


class CommandOut(BaseModel):
    command_id: uuid.UUID
    type: str
    parameters: dict
    version: int
    idempotency_key: str
    reason: str
    author: str
    valid_from: datetime
    expires_at: datetime


class CommandAckRequest(BaseModel):
    status: str = Field(..., description="accepted sau rejected")
    reason: str | None = None

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in ("accepted", "rejected"):
            raise ValueError("status trebuie sa fie 'accepted' sau 'rejected'")
        return v


class CommandResultRequest(BaseModel):
    status: str = Field(..., description="executed sau failed")
    observed_at: datetime | None = None
    details: dict = Field(default_factory=dict)
    error_message: str | None = None

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in ("executed", "failed"):
            raise ValueError("status trebuie sa fie 'executed' sau 'failed'")
        return v
