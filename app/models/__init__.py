"""Modele SQLAlchemy 2 pentru platforma EMS.

Import central pentru ca Alembic (autogenerate) si aplicatia sa vada
intregul metadata al bazei de date dintr-un singur punct.
"""
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.command import Command, CommandEvent
from app.models.device import ClaimCode, Device, DeviceCredential
from app.models.forecast import ConsumptionForecast, PvForecast, WeatherForecast
from app.models.market import ImportRun, MarketPriceInterval
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.organization import Membership, Organization
from app.models.preference import PreferenceVersion, StationPreference
from app.models.station import PanelGroup, Station, StationConfig, StationConfigVersion
from app.models.tariff import Tariff, TariffVersion
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.models.user import (
    Invitation,
    PasswordResetToken,
    Session as UserSession,
    User,
)

__all__ = [
    "Alert",
    "AuditLog",
    "Command",
    "CommandEvent",
    "ClaimCode",
    "Device",
    "DeviceCredential",
    "ConsumptionForecast",
    "PvForecast",
    "WeatherForecast",
    "ImportRun",
    "MarketPriceInterval",
    "OptimizationRun",
    "Plan",
    "PlanInterval",
    "Membership",
    "Organization",
    "PreferenceVersion",
    "StationPreference",
    "PanelGroup",
    "Station",
    "StationConfig",
    "StationConfigVersion",
    "Tariff",
    "TariffVersion",
    "TelemetryAggregate",
    "TelemetryRaw",
    "Invitation",
    "PasswordResetToken",
    "UserSession",
    "User",
]
