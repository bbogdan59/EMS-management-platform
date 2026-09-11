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


class AdminJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped_locked = "skipped_locked"
