"""Ciclul de viata al unei organizatii (client): profil, suspendare,
reactivare, arhivare si restaurare -- issue #24.

Tranzitii de stare valide (implementate strict, orice altceva e respins
explicit cu `OrganizationStateError`, nu ignorat tacit):

    active     -> suspended   (suspend_organization)
    active     -> archived    (archive_organization)
    suspended  -> active      (reactivate_organization)
    suspended  -> archived    (archive_organization)
    archived   -> active      (restore_organization)

Hard-delete e deliberat INDISPONIBIL in aceasta prima versiune (vezi
docs/LIMITATIONS.md) -- arhivarea ramane cea mai "finala" stare disponibila
din UI, si e recuperabila prin `restore_organization`.

Fiecare tranzitie:
  - e serializata per-organizatie printr-un advisory lock PostgreSQL
    transaction-scoped (acelasi tipar ca `optimization_service._run_locked`),
    ca doua cereri concurente de suspendare/reactivare sa nu produca stari
    inconsistente;
  - re-verifica starea CURENTA dupa achizitionarea lock-ului (nu doar
    inainte), impotriva unei tranzitii concurente deja aplicate;
  - scrie o intrare `AuditLog` (before/after status, motiv, actor) -- apelantul
    (ruta web) ramane responsabil de commit.

Suspendarea revoca imediat toate sesiunile web active ale membrilor
organizatiei (vezi `auth_service.revoke_all_sessions_for_users_in_organization`)
si, prin `Organization.status` verificat in `command_dispatch_service` si
`app/api/deps.py`, blocheaza dispecerizarea de comenzi live si actiunile de
scriere ale membrilor non-platform_admin. Accesul de tip viewer (read-only)
ramane permis cat timp organizatia e doar suspendata (nu arhivata) -- clientul
poate in continuare sa isi vada datele si sa exporte inainte de reactivare.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.security import utcnow
from app.models.organization import Organization
from app.models.user import User
from app.services import auth_service


class OrganizationStateError(Exception):
    pass


_VALID_TRANSITIONS = {
    ("active", "suspended"),
    ("active", "archived"),
    ("suspended", "active"),
    ("suspended", "archived"),
    ("archived", "active"),
}


def _lock_organization(db: Session, organization_id: uuid.UUID) -> None:
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"org-status:{organization_id}", 0))))


def _apply_transition(
    db: Session, organization: Organization, new_status: str, actor: User, *, reason: str | None = None
) -> Organization:
    _lock_organization(db, organization.id)
    db.refresh(organization)  # starea curenta, dupa achizitionarea lock-ului (nu cea citita inainte)

    old_status = organization.status
    if (old_status, new_status) not in _VALID_TRANSITIONS:
        raise OrganizationStateError(
            f"Tranzitie invalida: '{old_status}' -> '{new_status}' (organizatia {organization.id})."
        )

    organization.status = new_status
    now = utcnow()
    if new_status == "suspended":
        organization.suspended_at = now
        organization.suspended_reason = reason
        organization.suspended_by_user_id = actor.id
    elif new_status == "archived":
        organization.archived_at = now
        organization.archived_reason = reason
        organization.archived_by_user_id = actor.id
    db.add(organization)
    db.flush()

    record_audit(
        db,
        action=f"organization_{new_status}",
        resource_type="organization",
        resource_id=str(organization.id),
        actor_user_id=actor.id,
        actor_label=actor.email,
        organization_id=organization.id,
        metadata={"from_status": old_status, "to_status": new_status, "reason": reason},
    )
    return organization


def suspend_organization(db: Session, organization: Organization, actor: User, reason: str) -> Organization:
    if not reason or not reason.strip():
        raise OrganizationStateError("Motivul suspendarii este obligatoriu.")
    org = _apply_transition(db, organization, "suspended", actor, reason=reason.strip())
    revoked = auth_service.revoke_all_sessions_for_users_in_organization(db, organization.id)
    record_audit(
        db, action="organization_sessions_revoked", resource_type="organization", resource_id=str(organization.id),
        actor_user_id=actor.id, actor_label=actor.email, organization_id=organization.id,
        metadata={"revoked_session_count": revoked},
    )
    return org


def reactivate_organization(db: Session, organization: Organization, actor: User) -> Organization:
    return _apply_transition(db, organization, "active", actor)


def archive_organization(db: Session, organization: Organization, actor: User, reason: str) -> Organization:
    if not reason or not reason.strip():
        raise OrganizationStateError("Motivul arhivarii este obligatoriu.")
    org = _apply_transition(db, organization, "archived", actor, reason=reason.strip())
    revoked = auth_service.revoke_all_sessions_for_users_in_organization(db, organization.id)
    record_audit(
        db, action="organization_sessions_revoked", resource_type="organization", resource_id=str(organization.id),
        actor_user_id=actor.id, actor_label=actor.email, organization_id=organization.id,
        metadata={"revoked_session_count": revoked},
    )
    return org


def restore_organization(db: Session, organization: Organization, actor: User) -> Organization:
    return _apply_transition(db, organization, "active", actor)


def update_organization_profile(
    db: Session, organization: Organization, actor: User, *,
    name: str | None = None, billing_email: str | None = None, notes: str | None = None,
) -> Organization:
    """Editare de profil (fara schimbare de stare) -- nume, contact facturare,
    note administrative. Campurile omise (None) raman neschimbate; un sir gol
    explicit sterge campul (util pt. billing_email/notes opționale)."""
    before = {"name": organization.name, "billing_email": organization.billing_email, "notes": organization.notes}
    if name is not None and name.strip():
        organization.name = name.strip()
    if billing_email is not None:
        organization.billing_email = billing_email.strip() or None
    if notes is not None:
        organization.notes = notes.strip() or None
    db.add(organization)
    db.flush()
    record_audit(
        db, action="organization_profile_updated", resource_type="organization", resource_id=str(organization.id),
        actor_user_id=actor.id, actor_label=actor.email, organization_id=organization.id,
        metadata={
            "before": before,
            "after": {"name": organization.name, "billing_email": organization.billing_email, "notes": organization.notes},
        },
    )
    return organization
