from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.email import get_email_adapter
from app.core.security import (
    constant_time_eq,
    expires_in,
    generate_opaque_token,
    hash_password,
    hash_token,
    needs_rehash,
    utcnow,
    verify_password,
)
from app.models.enums import Role
from app.models.organization import Membership, Organization
from app.models.user import Invitation, PasswordResetToken, User
from app.models.user import Session as UserSession

settings = get_settings()

ORGANIZATION_ROLES = {Role.organization_admin.value, Role.operator.value, Role.viewer.value}


class AuthError(Exception):
    pass


class InvalidCredentials(AuthError):
    pass


class AccountLocked(AuthError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds


MAX_FAILED_ATTEMPTS = 8
LOCKOUT_MINUTES = 15


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if user is None or not user.is_active:
        raise InvalidCredentials()

    if user.locked_until and user.locked_until > utcnow():
        raise AccountLocked(retry_after_seconds=int((user.locked_until - utcnow()).total_seconds()))

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_ATTEMPTS:
            user.locked_until = expires_in(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
        db.add(user)
        db.flush()
        raise InvalidCredentials()

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.add(user)
    db.flush()
    return user


def create_session(db: Session, user: User, ip_address: str | None, user_agent: str | None) -> tuple[UserSession, str]:
    raw_token = generate_opaque_token()
    csrf_secret = generate_opaque_token(16)
    sess = UserSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        csrf_secret=csrf_secret,
        user_agent=(user_agent or "")[:500],
        ip_address=ip_address,
        expires_at=expires_in(hours=settings.session_ttl_hours),
    )
    db.add(sess)
    db.flush()
    return sess, raw_token


def revoke_session(db: Session, session: UserSession) -> None:
    session.revoked_at = utcnow()
    db.add(session)
    db.flush()


def revoke_all_sessions_for_user(db: Session, user_id: uuid.UUID, except_session_id: uuid.UUID | None = None) -> int:
    sessions = db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()
    count = 0
    for s in sessions:
        if s.revoked_at is None and s.id != except_session_id:
            s.revoked_at = utcnow()
            db.add(s)
            count += 1
    db.flush()
    return count


def revoke_all_sessions_for_users_in_organization(db: Session, organization_id: uuid.UUID) -> int:
    """Revoca toate sesiunile web active ale MEMBRILOR unei organizatii --
    folosit la suspendarea/arhivarea organizatiei (issue #24). Un utilizator
    poate avea membership in mai multe organizatii; doar sesiunea lui web
    (unica per browser) e revocata aici, nu apartenenta insasi -- daca are
    acces si prin alta organizatie neafectata, va trebui sa se re-autentifice,
    dar contul insusi ramane activ."""
    session_ids = db.scalars(
        select(UserSession.id)
        .join(Membership, Membership.user_id == UserSession.user_id)
        .where(Membership.organization_id == organization_id, UserSession.revoked_at.is_(None))
    ).all()
    count = 0
    for session_id in session_ids:
        sess = db.get(UserSession, session_id)
        if sess is not None and sess.revoked_at is None:
            sess.revoked_at = utcnow()
            db.add(sess)
            count += 1
    db.flush()
    return count


# --- Bootstrap primul admin ---


def bootstrap_first_admin(db: Session, provided_token: str, email: str, password: str, full_name: str) -> User:
    if not settings.bootstrap_admin_token:
        raise AuthError(
            "Bootstrap-ul este dezactivat: seteaza variabila de mediu BOOTSTRAP_ADMIN_TOKEN pentru a-l activa."
        )
    if not constant_time_eq(provided_token, settings.bootstrap_admin_token):
        raise AuthError("Token de bootstrap invalid.")

    existing_admin = db.scalar(select(User).where(User.is_platform_admin.is_(True)))
    if existing_admin is not None:
        raise AuthError("Exista deja un platform_admin; bootstrap-ul poate fi folosit o singura data.")

    user = User(
        email=email.lower().strip(),
        full_name=full_name,
        password_hash=hash_password(password),
        is_platform_admin=True,
    )
    db.add(user)
    db.flush()
    return user


# --- Invitatii ---


def create_invitation(
    db: Session, organization: Organization, email: str, role: str, invited_by_user_id: uuid.UUID
) -> tuple[Invitation, str]:
    """Creeaza (sau, daca exista deja una pending pentru aceeasi organizatie+
    email, ROTESTE) o invitatie. Reutilizarea randului existent -- in loc sa
    creeze un al doilea rand pending -- face operatia idempotenta la dublu
    submit sau la o reinvitare a cuiva deja invitat: ramane mereu cel mult o
    invitatie pending per (organization_id, email), cu un singur link valid
    la un moment dat (issue #149)."""
    if role not in ORGANIZATION_ROLES:
        raise AuthError("Rol de organizatie invalid.")
    normalized_email = email.lower().strip()
    raw_token = generate_opaque_token()

    existing = db.scalar(
        select(Invitation).where(
            Invitation.organization_id == organization.id,
            Invitation.email == normalized_email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    if existing is not None:
        existing.role = role
        existing.token_hash = hash_token(raw_token)
        existing.expires_at = expires_in(hours=settings.invite_token_ttl_hours)
        db.add(existing)
        db.flush()
        invitation = existing
    else:
        invitation = Invitation(
            organization_id=organization.id,
            email=normalized_email,
            role=role,
            token_hash=hash_token(raw_token),
            invited_by_user_id=invited_by_user_id,
            expires_at=expires_in(hours=settings.invite_token_ttl_hours),
        )
        db.add(invitation)
        db.flush()

    if settings.invitation_delivery_mode == "email":
        accept_url = f"{settings.base_url}/accept-invitation?token={raw_token}"
        get_email_adapter().send(
            to=invitation.email,
            subject=f"Invitatie EMS Platform - {organization.name}",
            body=(
                f"Ai fost invitat sa te alaturi organizatiei '{organization.name}' cu rolul '{role}'.\n"
                f"Acceseaza acest link pentru a-ti crea contul (valabil {settings.invite_token_ttl_hours}h):\n"
                f"{accept_url}"
            ),
        )
    # In modul 'manual_link' nu apelam deloc adaptorul de email -- URL-ul
    # complet e returnat apelantului (raw_token), care il afiseaza o singura
    # data unui actor autorizat; doar hash-ul ramane persistat mai sus.
    return invitation, raw_token


def accept_invitation(db: Session, raw_token: str, password: str, full_name: str) -> User:
    token_hash = hash_token(raw_token)
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash))
    if invitation is None or invitation.revoked_at is not None or invitation.accepted_at is not None:
        raise AuthError("Invitatie invalida sau deja folosita.")
    if invitation.expires_at < utcnow():
        raise AuthError("Invitatia a expirat.")

    user = db.scalar(select(User).where(User.email == invitation.email))
    if user is None:
        user = User(email=invitation.email, full_name=full_name, password_hash=hash_password(password))
        db.add(user)
        db.flush()

    existing_membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.organization_id == invitation.organization_id
        )
    )
    if existing_membership is None:
        db.add(Membership(user_id=user.id, organization_id=invitation.organization_id, role=invitation.role))

    invitation.accepted_at = utcnow()
    db.add(invitation)
    db.flush()
    return user


# --- Reset parola ---


def request_password_reset(db: Session, email: str) -> None:
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if user is None or not user.is_active:
        return  # nu dezvaluim daca emailul exista

    raw_token = generate_opaque_token()
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=expires_in(minutes=settings.password_reset_token_ttl_minutes),
    )
    db.add(reset)
    db.flush()

    reset_url = f"{settings.base_url}/reset-password?token={raw_token}"
    get_email_adapter().send(
        to=user.email,
        subject="Resetare parola - EMS Platform",
        body=(
            f"Am primit o cerere de resetare a parolei contului tau.\n"
            f"Link valabil {settings.password_reset_token_ttl_minutes} minute:\n{reset_url}\n"
            "Daca nu ai cerut aceasta resetare, ignora acest mesaj."
        ),
    )


def reset_password(db: Session, raw_token: str, new_password: str) -> User:
    token_hash = hash_token(raw_token)
    reset = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))
    if reset is None or reset.used_at is not None:
        raise AuthError("Token de resetare invalid sau deja folosit.")
    if reset.expires_at < utcnow():
        raise AuthError("Token de resetare expirat.")

    user = db.get(User, reset.user_id)
    if user is None:
        raise AuthError("Utilizator inexistent.")

    user.password_hash = hash_password(new_password)
    reset.used_at = utcnow()
    db.add_all([user, reset])
    revoke_all_sessions_for_user(db, user.id)
    db.flush()
    return user
