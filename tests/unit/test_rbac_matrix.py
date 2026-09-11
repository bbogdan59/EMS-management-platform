"""Verifica ca `app/core/rbac.py` respecta exact matricea documentata in
`docs/RBAC_MATRIX.md` (issue #23) -- daca cineva schimba pragul unei
capabilitati fara sa actualizeze documentul (sau invers), acest test pica."""
from __future__ import annotations

import pytest

from app.core import rbac
from app.models.enums import Role
from app.services.auth_service import ORGANIZATION_ROLES

ROLES = [Role.viewer, Role.operator, Role.organization_admin, Role.platform_admin]

# (functie, rol_minim_documentat) -- rolurile >= minim trebuie sa treaca,
# cele < minim trebuie respinse.
CAPABILITY_MINIMUMS = [
    (rbac.can_view_station, Role.viewer),
    (rbac.can_export_data, Role.viewer),
    (rbac.can_modify_operational_settings, Role.operator),
    (rbac.can_trigger_reoptimization, Role.operator),
    (rbac.can_manage_station_config, Role.organization_admin),
    (rbac.can_manage_organization, Role.organization_admin),
]


@pytest.mark.parametrize("capability,minimum", CAPABILITY_MINIMUMS)
def test_capability_matches_documented_minimum_role(capability, minimum):
    for role in ROLES:
        expected = rbac.role_at_least(role, minimum)
        assert capability(role.value) is expected, f"{capability.__name__}({role}) a fost {capability(role.value)}, asteptat {expected}"


def test_role_rank_is_strictly_ordered_as_documented():
    assert rbac._RANK[Role.viewer] < rbac._RANK[Role.operator] < rbac._RANK[Role.organization_admin] < rbac._RANK[Role.platform_admin]


def test_platform_admin_cannot_be_granted_via_organization_role_allowlist():
    """platform_admin e un flag global pe User, NU un rol de organizatie --
    allowlist-ul folosit de invitatii/schimbari de rol nu trebuie sa il
    contina niciodata, altfel s-ar putea acorda prin invitatie de organizatie."""
    assert Role.platform_admin.value not in ORGANIZATION_ROLES
    assert {Role.viewer.value, Role.operator.value, Role.organization_admin.value} == ORGANIZATION_ROLES


def test_can_reexecute_admin_jobs_is_platform_admin_only():
    assert rbac.can_reexecute_admin_jobs(True) is True
    assert rbac.can_reexecute_admin_jobs(False) is False
