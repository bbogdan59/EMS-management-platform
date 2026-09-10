"""Reguli de autorizare aplicate pe server, pentru fiecare resursa (inclusiv
SSE, exporturi, joburi si comenzi). Ascunderea unui buton in UI nu este
niciodata suficienta -- fiecare endpoint verifica explicit rolul."""
from __future__ import annotations

from app.models.enums import Role

_RANK = {
    Role.viewer: 0,
    Role.operator: 1,
    Role.organization_admin: 2,
    Role.platform_admin: 3,
}


def role_at_least(role: str | Role, minimum: str | Role) -> bool:
    r = Role(role)
    m = Role(minimum)
    return _RANK[r] >= _RANK[m]


def can_view_station(role: str) -> bool:
    return role_at_least(role, Role.viewer)


def can_modify_operational_settings(role: str) -> bool:
    """Preferinte, comenzi manuale, suspendare automatizare -- operator+."""
    return role_at_least(role, Role.operator)


def can_manage_station_config(role: str) -> bool:
    """Configuratie tehnica, tarife, dispozitive, membri -- doar admin de org+."""
    return role_at_least(role, Role.organization_admin)


def can_manage_organization(role: str) -> bool:
    return role_at_least(role, Role.organization_admin)


def can_export_data(role: str) -> bool:
    return role_at_least(role, Role.viewer)


def can_trigger_reoptimization(role: str) -> bool:
    return role_at_least(role, Role.operator)


def can_reexecute_admin_jobs(is_platform_admin: bool) -> bool:
    return is_platform_admin
