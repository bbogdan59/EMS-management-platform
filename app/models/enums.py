from __future__ import annotations

import enum


class Role(str, enum.Enum):
    platform_admin = "platform_admin"
    organization_admin = "organization_admin"
    operator = "operator"
    viewer = "viewer"


class DataQuality(str, enum.Enum):
    """Calitatea unei masuratori/valori afisate."""

    measured = "measured"
    estimated = "estimated"
    simulated = "simulated"
    stale = "stale"
    missing = "missing"


class DeviceStatus(str, enum.Enum):
    pending_claim = "pending_claim"
    active = "active"
    revoked = "revoked"


class ClaimCodeStatus(str, enum.Enum):
    pending = "pending"
    claimed = "claimed"
    expired = "expired"
    revoked = "revoked"


class CommandStatus(str, enum.Enum):
    created = "created"
    delivered = "delivered"
    accepted = "accepted"
    rejected = "rejected"
    executed = "executed"
    failed = "failed"
    expired = "expired"
    superseded = "superseded"


class CommandType(str, enum.Enum):
    """Comenzi semantice de nivel inalt -- niciodata scriere bruta de registre."""

    set_battery_target_soc = "set_battery_target_soc"
    set_charge_power_limit = "set_charge_power_limit"
    set_discharge_power_limit = "set_discharge_power_limit"
    hold_battery = "hold_battery"
    allow_grid_charge = "allow_grid_charge"
    disallow_grid_charge = "disallow_grid_charge"
    allow_export = "allow_export"
    disallow_export = "disallow_export"
    resume_automation = "resume_automation"
    suspend_automation = "suspend_automation"


class ImportRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    unpublished = "unpublished"


class OptimizationRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    infeasible = "infeasible"
    solver_timeout = "solver_timeout"
    failed = "failed"
    fallback = "fallback"


class PlanStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    accepted_by_device = "accepted_by_device"
    executing = "executing"
    completed = "completed"
    superseded = "superseded"


class ExecutionMode(str, enum.Enum):
    shadow = "shadow"
    live = "live"


class AlertSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class AlertStatus(str, enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class TariffDirection(str, enum.Enum):
    import_ = "import"
    export = "export"


class TariffKind(str, enum.Enum):
    fixed = "fixed"
    indexed_opcom = "indexed_opcom"


class OptimizationPriority(str, enum.Enum):
    cost = "cost"
    autonomy = "autonomy"
    battery_protection = "battery_protection"


class AdminJobType(str, enum.Enum):
    opcom_import = "opcom_import"
    optimization = "optimization"
    market_retention = "market_retention"


class AdminJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped_locked = "skipped_locked"


class TelemetrySource(str, enum.Enum):
    """Provenienta unui rand `TelemetryRaw` -- distinct de `DataQuality`
    (masurata/simulata/etc.), care descrie increderea in valoare, nu DE UNDE
    vine. `device_rs485` e valoarea implicita/istorica (dispozitiv EMS local,
    protocolul web-device existent); `deye_cloud` e telemetrie importata din
    contul Deye Cloud al clientului (issue #43), fara hardware EMS local."""

    device_rs485 = "device_rs485"
    deye_cloud = "deye_cloud"


class DeyeCloudConnectionStatus(str, enum.Enum):
    """Stare a unei conexiuni Deye Cloud pentru o statie (issue #43).

    `pending_selection`: autentificare reusita, dar clientul nu a ales inca
    CE statie din contul lui Deye Cloud sa importe (un cont poate avea mai
    multe). `connected`: statie aleasa, polling activ. `error`: autentificare
    esuata repetat (parola schimbata/revocata la Deye, cont blocat etc.) --
    polling-ul se opreste, dar conexiunea ramane vizibila in UI pentru
    reconectare, nu e stearsa silentios. `disconnected`: deconectat explicit
    de utilizator -- credentialele sunt sterse, istoricul de telemetrie deja
    importat RAMANE (nedistructiv, ca la arhivarea statiei)."""

    pending_selection = "pending_selection"
    connected = "connected"
    error = "error"
    disconnected = "disconnected"


class EquipmentType(str, enum.Enum):
    """Tip de echipament in catalogul administrabil -- issue #42. Catalogul
    comercial (aceasta lista) e distinct de harta de registre RS485 (issue
    #17, `InverterProfile`): a sti ca un invertor e "Deye SUN-10K-SG04LP3"
    nu implica automat ca stim cum sa ii controlam bateria prin Modbus."""

    inverter = "inverter"
    battery = "battery"
    pv_module = "pv_module"
