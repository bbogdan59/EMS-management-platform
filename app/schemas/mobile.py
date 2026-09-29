"""Canonical mobile read contract. Decimal values serialize as JSON strings."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Quality = Literal[
    "measured", "estimated", "declared", "simulated", "stale", "partial", "missing", "unknown"
]
IssueCode = Literal[
    "unauthenticated",
    "forbidden",
    "no_station",
    "station_required",
    "integration_disconnected",
    "provider_disconnected",
    "reauth_required",
    "stale",
    "rate_limited",
    "invalid_request",
    "invalid_cursor",
    "service_unavailable",
]


class MobileIssue(BaseModel):
    code: IssueCode
    message: str
    resource: str | None = None
    retry_after_seconds: int | None = None


class MobileError(BaseModel):
    schema_version: Literal[1] = 1
    error: MobileIssue


class MobileMetric(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    value: Decimal | bool | str | None
    unit: str | None
    source: str | None
    quality: Quality
    measured_at: datetime | None = None
    received_at: datetime | None = None
    updated_at: datetime | None = None
    coverage: Decimal | None = None
    flags: list[str] = Field(default_factory=list)


class MobileComparison(BaseModel):
    previous_value: Decimal
    delta: Decimal
    delta_percent: Decimal | None
    quality: Quality


class MobileEnergyMetric(MobileMetric):
    comparison: MobileComparison | None = None


class MobilePeriod(BaseModel):
    start: datetime
    end: datetime
    comparison_label: str
    metrics: dict[str, MobileEnergyMetric]


class MobileStation(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    timezone: str
    currency: Literal["RON"] = "RON"
    role: str


class MobileDevice(BaseModel):
    id: UUID
    name: str
    status: str
    source: str
    last_heartbeat_at: datetime | None
    measured_at: datetime | None
    received_at: datetime | None
    quality: Quality
    freshness: Literal["fresh", "stale", "missing"]


class MobileSourceSummary(BaseModel):
    devices: list[MobileDevice]
    total_devices: int
    truncated: bool


class MobileIntegration(BaseModel):
    provider: Literal["deye_cloud", "home_assistant_bridge", "home_assistant_mqtt"]
    status: str
    enabled: bool
    last_seen_at: datetime | None
    mapping_version: int | None = None
    entity_count: int = 0
    available_entities: int = 0
    issues: list[MobileIssue] = Field(default_factory=list)


class MobileForecast(BaseModel):
    start: datetime
    end: datetime
    pv_energy: MobileMetric
    confidence: str | None
    issued_at: datetime | None
    method: str = "latest_expected_pv_forecast_integral/v1"


class MobileCost(BaseModel):
    net_cost: MobileMetric
    system_benefit: MobileMetric
    currency: Literal["RON"] = "RON"
    reason: str | None
    method: str = "historical_hourly_import_minus_export/v1"
    excludes: list[str] = Field(
        default_factory=lambda: ["fixed_fees", "monthly_netting", "invoice_projection"]
    )


class MobileOverview(BaseModel):
    schema_version: Literal[1] = 1
    evaluated_at: datetime
    station: MobileStation
    live: dict[str, MobileMetric]
    energy: dict[Literal["today", "month"], MobilePeriod]
    forecast: MobileForecast
    costs: dict[Literal["today", "month"], MobileCost]
    unread_notifications: int
    sources: MobileSourceSummary
    integrations: list[MobileIntegration]
    issues: list[MobileIssue]


class MobileChartPoint(BaseModel):
    start: datetime
    end: datetime
    values: dict[str, Decimal | None]
    coverage: dict[str, Decimal]
    quality: Quality
    updated_at: datetime | None


class MobileChart(BaseModel):
    schema_version: Literal[1] = 1
    station_id: UUID
    timezone: str
    start: datetime
    end: datetime
    resolution: Literal["15m", "1h", "1d"]
    units: dict[str, str]
    aggregation: dict[str, str]
    source: str = "telemetry_aggregate"
    measured_at: datetime | None = None
    received_at: datetime | None = None
    points: list[MobileChartPoint]


class MobileNotification(BaseModel):
    id: UUID
    title: str
    body: str | None
    category: str
    severity: str
    state: str | None
    created_at: datetime
    read_at: datetime | None
    link: str | None


class MobileNotifications(BaseModel):
    schema_version: Literal[1] = 1
    station_id: UUID
    items: list[MobileNotification]
    unread_count: int
    next_cursor: str | None


class MobileChargingSession(BaseModel):
    id: UUID
    connector_id: UUID
    vehicle_id: UUID | None
    started_at: datetime
    ended_at: datetime | None
    state: str
    energy: MobileMetric
    reason_codes: list[str]


class MobileChargingSessions(BaseModel):
    schema_version: Literal[1] = 1
    station_id: UUID
    items: list[MobileChargingSession]
    next_cursor: str | None


class MobileHAEntity(BaseModel):
    entity_id: str
    kind: str
    mapped_unit: str
    max_age_seconds: int
    available: bool
    source_quality: Quality
    is_stale: bool
    is_simulated: bool
    metric: MobileMetric
    control_authorized: Literal[False] = False


class MobileHAContext(BaseModel):
    schema_version: Literal[1] = 1
    station_id: UUID
    timezone: str
    integration: MobileIntegration
    entities: list[MobileHAEntity]
    insights_consent: bool
    physical_control: Literal[False] = False
