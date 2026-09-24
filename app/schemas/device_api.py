"""Scheme Pydantic pentru API-ul v1 destinat dispozitivului (viitorul
controler local). Documentate integral in docs/API.md, cu exemple JSON."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TelemetryQuality = Literal["measured", "derived", "simulated", "stale"]


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
    system_stats: dict = Field(
        default_factory=dict,
        description=(
            "Instantaneu al resurselor sistemului RAPORTAT de dispozitiv (ex. "
            "cpu_load_1m, memory_used_percent, memory_total_mb, temperature_c, "
            "disk_used_percent). Un camp necunoscut dispozitivului lipseste, nu "
            "este 0. Inlocuieste (nu combina) instantaneul anterior."
        ),
    )
    # Tipizate (issue #168), simetrice cu EnrollRequest -- optionale: un
    # device vechi, pre-#168, nu le trimite si nu trebuie sa esueze.
    build_id: str | None = Field(default=None, max_length=64)
    hardware_platform: str | None = Field(default=None, max_length=64)
    architecture: str | None = Field(default=None, max_length=32)
    os_version: str | None = Field(default=None, max_length=64)


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

    boot_id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(..., ge=0, le=9223372036854775807, description="Contor monoton crescator in cadrul unui boot_id, pentru deduplicare.")
    schema_version: int = Field(default=1, ge=1)
    measured_at: datetime

    pv_power_w: Decimal | None = Field(default=None, description="W, >=0. Respingere semantica per item daca este negativa.")
    load_power_w: Decimal | None = Field(default=None, description="W, >=0. Respingere semantica per item daca este negativa.")
    battery_power_w: Decimal | None = Field(default=None, description="W; >0 incarcare, <0 descarcare.")
    grid_power_w: Decimal | None = Field(default=None, description="W; >0 import, <0 export.")
    battery_soc_percent: Decimal | None = Field(default=None, description="%, interval valid 0..100; 0 este valoare masurata valida.")
    ev_connected: bool | None = None
    ev_power_w: Decimal | None = Field(default=None, description="W, >=0. Respingere semantica per item daca este negativa.")

    mppt: list[MpptTelemetry] = Field(default_factory=list, max_length=8)
    phases: list[PhaseTelemetry] = Field(default_factory=list, max_length=6)
    battery: BatteryTelemetry | None = None
    inverter: InverterTelemetry | None = None
    status: DeviceStatusTelemetry | None = None
    counters: list[CumulativeCounterTelemetry] = Field(default_factory=list, max_length=32)

    quality_flags: dict = Field(default_factory=dict)
    raw_payload: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _agent_flat_metrics(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        groups = {}
        for index in (1, 2):
            row = {}
            for metric, unit in (("power", "w"), ("voltage", "v"), ("current", "a")):
                key = f"pv{index}_{metric}_{unit}"
                if key in data:
                    row[f"{metric}_{unit}"] = data.pop(key)
            if row:
                groups.setdefault("mppt", []).append({"index": index, **row})
        for circuit in ("grid", "load"):
            for phase in ("l1", "l2", "l3"):
                row = {}
                for key, target in ((f"{circuit}_voltage_{phase}_v", "voltage_v"),
                                    (f"grid_ct_{phase}_w" if circuit == "grid" else f"load_power_{phase}_w", "active_power_w")):
                    if key in data:
                        row[target] = data.pop(key)
                if row:
                    groups.setdefault("phases", []).append({"circuit": circuit, "phase": phase.upper(), **row})
        for group, mapping in (("battery", {"battery_voltage_v": "voltage_v", "battery_current_a": "current_a", "battery_temperature_c": "temperature_c"}),
                               ("inverter", {"dc_temperature_c": "dc_temperature_c", "ac_temperature_c": "ac_temperature_c", "inverter_status_code": "status_code"})):
            row = {target: data.pop(key) for key, target in mapping.items() if key in data}
            if row:
                groups[group] = row
        for name in ("pv", "load", "grid_import", "grid_export", "battery_charge", "battery_discharge"):
            key = f"{name}_energy_total_kwh"
            if key in data:
                number = data.pop(key)
                if number is not None:
                    groups.setdefault("counters", []).append({"name": f"{name}_energy_total", "unit": "kWh", "value": number})
        for group, normalized in groups.items():
            if group in data:
                raise ValueError(f"Nu combina metricile agentului cu grupul tipizat {group}.")
            data[group] = normalized
        return data

    @field_validator("measured_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("measured_at trebuie sa includa fusul orar (ex. sufix Z sau +02:00).")
        return v


class MpptTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    index: int = Field(..., ge=1, description="Indexul MPPT raportat de device, 1-based.")
    voltage_v: Decimal | None = Field(default=None, ge=0)
    current_a: Decimal | None = Field(default=None, ge=0)
    power_w: Decimal | None = Field(default=None, ge=0)
    quality: TelemetryQuality = "measured"


class PhaseTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    phase: Literal["L1", "L2", "L3"]
    circuit: Literal["grid", "load"] = "grid"
    voltage_v: Decimal | None = Field(default=None, ge=0)
    current_a: Decimal | None = Field(default=None, ge=0)
    active_power_w: Decimal | None = Field(
        default=None,
        description="W; aceeasi conventie de semn ca metrica pe care o detaliaza, cand este aplicabil.",
    )
    quality: TelemetryQuality = "measured"


class BatteryTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    voltage_v: Decimal | None = Field(default=None, ge=0)
    current_a: Decimal | None = Field(default=None)
    temperature_c: Decimal | None = None
    soh_percent: Decimal | None = Field(default=None, ge=0, le=100)
    state: Literal["idle", "charging", "discharging", "fault", "unknown"] | None = None
    quality: TelemetryQuality = "measured"


class InverterTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    dc_temperature_c: Decimal | None = None
    ac_temperature_c: Decimal | None = None
    status_code: int | None = Field(default=None, ge=0, le=65535)
    quality: TelemetryQuality = "measured"


class FaultTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=64)
    severity: Literal["info", "warning", "error", "critical"]
    message: str | None = Field(default=None, max_length=200)


class DeviceStatusTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inverter_state: Literal["offline", "standby", "running", "fault", "unknown"] | None = None
    battery_state: Literal["idle", "charging", "discharging", "fault", "unknown"] | None = None
    faults: list[FaultTelemetry] = Field(default_factory=list, max_length=32)
    quality: Literal["reported", "measured", "derived", "simulated", "stale"] = "reported"


class CumulativeCounterTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: Literal[
        "pv_energy_total",
        "load_energy_total",
        "grid_import_energy_total",
        "grid_export_energy_total",
        "battery_charge_energy_total",
        "battery_discharge_energy_total",
        "ev_energy_total",
    ]
    value: Decimal = Field(..., ge=0)
    unit: Literal["kWh"]
    reset_id: str | None = Field(
        default=None,
        max_length=64,
        description="Schimbat de device cand contorul a fost resetat sau a facut rollover.",
    )
    quality: TelemetryQuality = "measured"

    rollover_kwh: Decimal | None = Field(default=None, gt=0)


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


class DeviceLogEntryIn(BaseModel):
    """O linie de jurnal COMPACTA (nu un stack trace/payload) -- gandita
    pentru debugging live pe pagina de configurare a device-ului, cu
    retentie de 10 zile pe server (vezi device_service.ingest_device_logs)."""

    model_config = ConfigDict(extra="forbid")

    occurred_at: datetime
    level: Literal["info", "warning", "error"]
    code: str = Field(..., max_length=64)
    detail: str | None = Field(default=None, max_length=200)

    @field_validator("occurred_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at trebuie sa includa fusul orar (ex. sufix Z sau +02:00).")
        return v


class DeviceLogBatchRequest(BaseModel):
    entries: list[DeviceLogEntryIn] = Field(default_factory=list, max_length=50)


class DeviceLogBatchResponse(BaseModel):
    accepted: int


class TelemetryMetricSpec(BaseModel):
    name: str
    unit: str | None
    nullable: bool
    quality: Literal["measured", "reported", "derived"]
    description: str
    sign: str | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None


class TelemetryContractResponse(BaseModel):
    schema_version: int
    supported_schema_versions: list[int] = Field(default_factory=lambda: [1, 2])
    endpoint: str
    deduplication_key: list[str]
    time: dict
    metrics: list[TelemetryMetricSpec]
    quality_flags: dict
    extended_metrics: dict
    cumulative_counters: dict
    provenance: dict
    raw_payload: dict
    ack: dict


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


# --- Firmware OTA (issue #168) -- strictly a release_id/manifest target, ---
# --- never a shell command, raw URL or executable argument.              ---


class FirmwareOfferOut(BaseModel):
    deployment_id: uuid.UUID
    release_id: uuid.UUID
    target_version: str
    channel: str
    status: str
    is_downgrade: bool
    offer_expires_at: datetime
    download_url: str = Field(..., description="Presemnata, cu durata limitata -- niciodata un link permanent/public.")
    sha256_hex: str
    signature_ed25519_hex: str
    signing_key_id: str
    artifact_size_bytes: int


class FirmwareDeploymentEventRequest(BaseModel):
    event_type: Literal[
        "downloading", "verified", "installing", "restarting", "confirmed", "failed", "rejected",
    ]
    payload: dict = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=1000)
