"""Administrarea membership-urilor si invitatiilor unei organizatii (issue
#23): listare, schimbare de rol, dezactivare/reactivare/eliminare
membership, retrimitere/anulare invitatie -- toate cu protectia ultimului
`organization_admin` activ si audit explicit.

Nicio functie de aici NU decide autorizarea (asta ramane in
`app/api/deps.py`/`app/core/rbac.py`, verificata la fiecare endpoint) --
doar aplica regulile de business odata ce apelantul a trecut deja acel
control. `is_active=False` pe o membership (vezi modelul) e tratat peste
tot ca "fara acces", identic cu lipsa membership-ului."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.security import expires_in, generate_opaque_token, hash_token, utcnow
from app.models.organization import Membership, Organization
from app.models.user import Invitation, User
from app.services import auth_service
from app.services.auth_service import ORGANIZATION_ROLES

MANAGER_ROLE = "organization_admin"


class MembershipError(Exception):
    pass


def _actor_label(actor: User) -> str:
    return actor.email


def count_active_admins(db: Session, organization_id: uuid.UUID, *, exclude_membership_id: uuid.UUID | None = None) -> int:
    stmt = select(Membership).where(
        Membership.organization_id == organization_id,
        Membership.role == MANAGER_ROLE,
        Membership.is_active.is_(True),
    )
    if exclude_membership_id is not None:
        stmt = stmt.where(Membership.id != exclude_membership_id)
    return len(db.scalars(stmt).all())


def _lock_organization_memberships(db: Session, organization_id: uuid.UUID) -> None:
    """Serialize membership mutations for one organization.

    The last-admin check is otherwise vulnerable to a write-skew race: two
    administrators can be demoted concurrently after both observe the other
    one as active. PostgreSQL row locks are held until the caller commits.
    """
    db.execute(
        select(Membership.id)
        .where(Membership.organization_id == organization_id)
        .order_by(Membership.id)
        .with_for_update()
    ).all()


def _assert_not_last_admin(db: Session, membership: Membership) -> None:
    """Interzice orice tranzitie (demotare, dezactivare, eliminare) care ar
    lasa organizatia FARA niciun `organization_admin` activ -- fara asta,
    nimeni din organizatie nu ar mai putea administra membrii/statiile,
    nici macar un platform_admin nu ar afla cine ar trebui sa preia rolul."""
    if membership.role != MANAGER_ROLE or not membership.is_active:
        return
    if count_active_admins(db, membership.organization_id, exclude_membership_id=membership.id) == 0:
        raise MembershipError(
            "Nu poti elimina/dezactiva/retrograda ultimul organization_admin activ al organizatiei. "
            "Promoveaza mai intai un alt membru la organization_admin."
        )


def list_members(db: Session, organization: Organization) -> list[dict]:
    memberships = db.scalars(
        select(Membership).where(Membership.organization_id == organization.id).order_by(Membership.created_at)
    ).all()
    rows = []
    for m in memberships:
        user = db.get(User, m.user_id)
        rows.append(
            {
                "membership_id": m.id,
                "user_id": m.user_id,
                "email": user.email if user else "?",
                "full_name": user.full_name if user else "?",
                "role": m.role,
                "is_active": m.is_active,
                "is_sole_admin": m.role == MANAGER_ROLE
                and m.is_active
                and count_active_admins(db, organization.id, exclude_membership_id=m.id) == 0,
            }
        )
    return rows


def list_pending_invitations(db: Session, organization: Organization) -> list[Invitation]:
    """Invitatii care nu au fost inca acceptate si nu au fost anulate -- include
    si cele expirate (UI-ul le marcheaza distinct, ca sa poata fi retrimise
    fara sa fie ambigue cu una inca valabila)."""
    return db.scalars(
        select(Invitation)
        .where(
            Invitation.organization_id == organization.id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .order_by(Invitation.created_at.desc())
    ).all()


def change_role(db: Session, organization: Organization, membership: Membership, new_role: str, actor: User) -> Membership:
    if membership.organization_id != organization.id:
        raise MembershipError("Membership nu apartine acestei organizatii.")
    _lock_organization_memberships(db, organization.id)
    db.refresh(membership)
    if new_role not in ORGANIZATION_ROLES:
        raise MembershipError(f"Rol invalid: '{new_role}'.")
    if membership.role == new_role:
        return membership

    old_role = membership.role
    if old_role == MANAGER_ROLE and new_role != MANAGER_ROLE:
        _assert_not_last_admin(db, membership)

    membership.role = new_role
    db.add(membership)
    db.flush()

    # Schimbare sensibila -- forteaza re-autentificarea membrului afectat, ca
    # noul rol sa se aplice imediat (nu doar la urmatoarea expirare naturala
    # a sesiunii).
    auth_service.revoke_all_sessions_for_user(db, membership.user_id)

    record_audit(
        db, action="membership_role_changed", resource_type="membership", resource_id=str(membership.id),
        actor_user_id=actor.id, actor_label=_actor_label(actor), organization_id=organization.id,
        metadata={"user_id": str(membership.user_id), "from_role": old_role, "to_role": new_role},
    )
    return membership


def deactivate_member(db: Session, organization: Organization, membership: Membership, actor: User) -> Membership:
    if membership.organization_id != organization.id:
        raise MembershipError("Membership nu apartine acestei organizatii.")
    _lock_organization_memberships(db, organization.id)
    db.refresh(membership)
    if not membership.is_active:
        return membership
    _assert_not_last_admin(db, membership)

    membership.is_active = False
    db.add(membership)
    db.flush()
    auth_service.revoke_all_sessions_for_user(db, membership.user_id)

    record_audit(
        db, action="membership_deactivated", resource_type="membership", resource_id=str(membership.id),
        actor_user_id=actor.id, actor_label=_actor_label(actor), organization_id=organization.id,
        metadata={"user_id": str(membership.user_id), "role": membership.role},
    )
    return membership


def reactivate_member(db: Session, organization: Organization, membership: Membership, actor: User) -> Membership:
    if membership.organization_id != organization.id:
        raise MembershipError("Membership nu apartine acestei organizatii.")
    _lock_organization_memberships(db, organization.id)
    db.refresh(membership)
    if membership.is_active:
        return membership

    membership.is_active = True
    db.add(membership)
    db.flush()

    record_audit(
        db, action="membership_reactivated", resource_type="membership", resource_id=str(membership.id),
        actor_user_id=actor.id, actor_label=_actor_label(actor), organization_id=organization.id,
        metadata={"user_id": str(membership.user_id), "role": membership.role},
    )
    return membership


def remove_member(db: Session, organization: Organization, membership: Membership, actor: User) -> None:
    """Eliminare COMPLETA (hard delete) -- pentru corectarea unei invitatii/
    membership create din greseala, nu ca mecanism normal de offboarding
    (foloseste `deactivate_member` pentru asta, reversibil si cu istoric)."""
    if membership.organization_id != organization.id:
        raise MembershipError("Membership nu apartine acestei organizatii.")
    _lock_organization_memberships(db, organization.id)
    db.refresh(membership)
    _assert_not_last_admin(db, membership)

    user_id = membership.user_id
    role = membership.role
    membership_id = membership.id
    db.delete(membership)
    db.flush()
    auth_service.revoke_all_sessions_for_user(db, user_id)

    record_audit(
        db, action="membership_removed", resource_type="membership", resource_id=str(membership_id),
        actor_user_id=actor.id, actor_label=_actor_label(actor), organization_id=organization.id,
        metadata={"user_id": str(user_id), "role": role},
    )


def resend_invitation(db: Session, organization: Organization, invitation: Invitation, actor: User) -> str:
    """Regenereaza token-ul si prospetimea unei invitatii existente (in loc
    sa creeze una noua) -- pastreaza acelasi `Invitation.id`, deci acelasi
    istoric de audit. In modul 'email' retrimite mesajul cu noul link; in
    modul 'manual_link' (issue #149) nu atinge deloc adaptorul de email --
    apelantul (ruta HTTP) afiseaza URL-ul complet returnat aici o singura
    data. In ambele moduri, rotatia INVALIDEAZA imediat linkul anterior:
    hash-ul vechi nu mai exista in DB dupa acest apel, deci `accept_invitation`
    cu tokenul vechi va esua."""
    if invitation.organization_id != organization.id:
        raise MembershipError("Invitatia nu apartine acestei organizatii.")
    if invitation.accepted_at is not None:
        raise MembershipError("Invitatia a fost deja acceptata.")
    if invitation.revoked_at is not None:
        raise MembershipError("Invitatia a fost anulata.")

    from app.config import get_settings

    settings = get_settings()
    raw_token = generate_opaque_token()
    invitation.token_hash = hash_token(raw_token)
    invitation.expires_at = expires_in(hours=settings.invite_token_ttl_hours)
    db.add(invitation)
    db.flush()

    if settings.invitation_delivery_mode == "email":
        from app.core.email import get_email_adapter

        accept_url = f"{settings.base_url}/accept-invitation?token={raw_token}"
        get_email_adapter().send(
            to=invitation.email,
            subject=f"Invitatie EMS Platform - {organization.name}",
            body=(
                f"Ai fost invitat sa te alaturi organizatiei '{organization.name}' cu rolul '{invitation.role}'.\n"
                f"Acceseaza acest link pentru a-ti crea contul (valabil {settings.invite_token_ttl_hours}h):\n"
                f"{accept_url}"
            ),
        )

    record_audit(
        db, action="invitation_resent", resource_type="invitation", resource_id=str(invitation.id),
        actor_user_id=actor.id, actor_label=_actor_label(actor), organization_id=organization.id,
        metadata={
            "invited_email": invitation.email,
            "role": invitation.role,
            "delivery_mode": settings.invitation_delivery_mode,
        },
    )
    return raw_token


def cancel_invitation(db: Session, organization: Organization, invitation: Invitation, actor: User) -> Invitation:
    if invitation.organization_id != organization.id:
        raise MembershipError("Invitatia nu apartine acestei organizatii.")
    if invitation.accepted_at is not None:
        raise MembershipError("Invitatia a fost deja acceptata -- nu mai poate fi anulata.")
    if invitation.revoked_at is not None:
        return invitation

    invitation.revoked_at = utcnow()
    db.add(invitation)
    db.flush()

    record_audit(
        db, action="invitation_cancelled", resource_type="invitation", resource_id=str(invitation.id),
        actor_user_id=actor.id, actor_label=_actor_label(actor), organization_id=organization.id,
        metadata={"invited_email": invitation.email, "role": invitation.role},
    )
    return invitation
