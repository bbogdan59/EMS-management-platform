
"""Modele SQLAlchemy 2 pentru platforma EMS.

Import central pentru ca Alembic (autogenerate) si aplicatia sa vada
intregul metadata al bazei de date dintr-un singur punct.
"""
from app.models.admin_job import AdminJob
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.command import Command, CommandEvent
from app.models.device import ClaimCode, Device, DeviceCredential
from app.models.deye_integration import DeyeCloudConnection, DeyeCloudDeviceLink
from app.models.equipment_catalog import EquipmentManufacturer, EquipmentModel
from app.models.forecast import ConsumptionForecast, PvForecast, WeatherForecast
from app.models.inverter_config import InverterDesired, InverterProfile, InverterReport
from app.models.market import ImportRun, MarketPriceInterval
from app.models.optimization import OptimizationRun, Plan, PlanInterval
from app.models.organization import Membership, Organization
from app.models.preference import PreferenceVersion, StationPreference
from app.models.station import PanelGroup, Station, StationConfig, StationConfigVersion
from app.models.tariff import Tariff, TariffVersion
from app.models.task_execution import TaskExecution
from app.models.telemetry import TelemetryAggregate, TelemetryRaw
from app.models.user import (
    Invitation,
    PasswordResetToken,
    User,
)
from app.models.user import (
    Session as UserSession,
)

__all__ = [
    "AdminJob",
    "Alert",
    "AuditLog",
    "ClaimCode",
    "Command",
    "CommandEvent",
    "ConsumptionForecast",
    "Device",
    "DeviceCredential",
    "DeyeCloudConnection",
    "DeyeCloudDeviceLink",
    "EquipmentManufacturer",
    "EquipmentModel",
    "ImportRun",
    "InverterDesired",
    "InverterProfile",
    "InverterReport",
    "Invitation",
    "MarketPriceInterval",
    "Membership",
    "OptimizationRun",
    "Organization",
    "PanelGroup",
    "PasswordResetToken",
    "Plan",
    "PlanInterval",
    "PreferenceVersion",
    "PvForecast",
    "Station",
    "StationConfig",
    "StationConfigVersion",
    "StationPreference",
    "Tariff",
    "TariffVersion",
    "TaskExecution",
    "TelemetryAggregate",
    "TelemetryRaw",
    "User",
    "UserSession",
    "WeatherForecast",
]
