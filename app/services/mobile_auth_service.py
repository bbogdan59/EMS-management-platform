"""Opaque, installation-bound credentials. Callers commit even rejected replays."""

import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.email import get_email_adapter
from app.core.security import (
    constant_time_eq,
    generate_opaque_token,
    hash_password,
    hash_token,
    utcnow,
    verify_password,
)
from app.models.mobile_auth import MobileRefreshToken, MobileRegistration, MobileSession
from app.models.organization import Membership, Organization
from app.models.user import Invitation, PasswordResetToken, User
from app.schemas.mobile_auth import (
    Installation,
    LoginInput,
    MobileIdentity,
    MobileSessionInfo,
    MobileTokens,
    MobileUser,
    VerifySignupInput,
)

ACCESS_SECONDS = 300
SESSION_DAYS = 30
MAX_SESSIONS = 10
_DUMMY_HASH = hash_password(generate_opaque_token())


class MobileAuthError(Exception):
    def __init__(self, code: str, status: int = 401, retry_after: int | None = None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


def email_available() -> bool:
    settings = get_settings()
    return settings.email_deliverable and bool(settings.smtp_host)


def send_email(email: str, body: str) -> None:
    if not email_available():
        raise MobileAuthError("email_unavailable", 503)
    try:
        get_email_adapter().send(to=email, subject="Your EMS account", body=body)
    except Exception as exc:
        # Neither the exception (which may contain recipients/credentials) nor body is logged.
        raise MobileAuthError("email_unavailable", 503) from exc


def email_lock(db: Session, email: str) -> None:
    # Serializes first registrations before a row exists, across API workers.
    key = int(hash_token(email)[:15], 16)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email).with_for_update())
    valid = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    now = utcnow()
    if not user or not user.is_active or (user.locked_until and user.locked_until > now):
        raise MobileAuthError("invalid_credentials")
    if not valid:
        user.failed_login_count += 1
        if user.failed_login_count >= 8:
            user.locked_until = now + timedelta(minutes=15)
            user.failed_login_count = 0
        raise MobileAuthError("invalid_credentials")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    return user


def session_info(session: MobileSession, current_id: UUID) -> MobileSessionInfo:
    return MobileSessionInfo(
        id=session.id,
        device_name=session.device_name,
        platform=session.platform,
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        expires_at=session.expires_at,
        current=session.id == current_id,
    )


def identity(user: User, session: MobileSession) -> MobileIdentity:
    return MobileIdentity(
        user=MobileUser(id=user.id, email=user.email, full_name=user.full_name),
        session=session_info(session, session.id),
    )


def rotate(db: Session, user: User, session: MobileSession) -> MobileTokens:
    access, refresh = generate_opaque_token(), generate_opaque_token()
    session.access_hash = hash_token(access)
    session.access_expires_at = utcnow() + timedelta(seconds=ACCESS_SECONDS)
    session.last_seen_at = utcnow()
    db.add(session)
    db.flush()
    db.add(MobileRefreshToken(session_id=session.id, token_hash=hash_token(refresh)))
    return MobileTokens(
        **identity(user, session).model_dump(),
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_SECONDS,
    )


def create_session(db: Session, user: User, data: LoginInput) -> MobileTokens:
    # User row is locked by authenticate/verification. Replace this installation's
    # previous session and cap the active set, without dropping spent-token history.
    active = db.scalars(
        select(MobileSession)
        .where(
            MobileSession.user_id == user.id,
            MobileSession.revoked_at.is_(None),
            MobileSession.expires_at > utcnow(),
        )
        .order_by(MobileSession.created_at.desc())
        .with_for_update()
    ).all()
    kept = 0
    for session in active:
        if session.installation_id == data.installation_id or kept >= MAX_SESSIONS - 1:
            session.revoked_at = utcnow()
        else:
            kept += 1
    session = MobileSession(
        user_id=user.id,
        installation_id=data.installation_id,
        installation_key_hash=hash_token(data.installation_key),
        device_name=data.device_name.strip(),
        platform=data.platform,
        expires_at=utcnow() + timedelta(days=SESSION_DAYS),
    )
    return rotate(db, user, session)


def bound(session: MobileSession, installation: Installation) -> bool:
    return session.installation_id == installation.installation_id and constant_time_eq(
        session.installation_key_hash,
        hash_token(installation.installation_key),
    )


def refresh_session(
    db: Session, raw: str, installation: Installation, *, logout: bool = False
) -> MobileTokens | None:
    token = db.scalar(
        select(MobileRefreshToken).where(MobileRefreshToken.token_hash == hash_token(raw))
    )
    session = db.get(MobileSession, token.session_id) if token else None
    if not session or not token:
        raise MobileAuthError("invalid_session")
    # Same user -> session lock order as login/revocation. Re-read after waiting:
    # the identity map may hold the previous rotation's values.
    user = db.scalar(
        select(User)
        .where(User.id == session.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session = db.scalar(
        select(MobileSession)
        .where(MobileSession.id == session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    db.refresh(token)
    if not session or not bound(session, installation):
        raise MobileAuthError("invalid_session")
    if logout:
        session.revoked_at = utcnow()
        return None
    if token.used_at is not None or not user or not user.is_active:
        session.revoked_at = utcnow()
        raise MobileAuthError("invalid_session")
    if session.revoked_at or session.expires_at <= utcnow():
        raise MobileAuthError("invalid_session")
    token.used_at = utcnow()
    return rotate(db, user, session)


def authorize(db: Session, access: str, installation: Installation) -> tuple[User, MobileSession]:
    session = db.scalar(
        select(MobileSession).where(MobileSession.access_hash == hash_token(access))
    )
    if (
        not session
        or not bound(session, installation)
        or session.revoked_at
        or session.expires_at <= utcnow()
        or session.access_expires_at <= utcnow()
    ):
        raise MobileAuthError("invalid_session")
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        raise MobileAuthError("invalid_session")
    return user, session


def revoke_for_user(db: Session, user_id: UUID) -> int:
    result = db.execute(
        update(MobileSession)
        .where(
            MobileSession.user_id == user_id,
            MobileSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    return result.rowcount


def purge_expired(db: Session, now=None) -> dict[str, int]:
    """Delete expired auth families and pending registrations.

    Spent refresh-token history is retained until its session reaches the
    absolute expiry, preserving replay detection for every live family.
    """
    now = now or utcnow()
    registrations = db.execute(
        delete(MobileRegistration).where(MobileRegistration.expires_at < now)
    ).rowcount
    sessions = db.execute(delete(MobileSession).where(MobileSession.expires_at < now)).rowcount
    return {"sessions": sessions, "registrations": registrations}


def request_signup(db: Session, email: str) -> None:
    if not email_available():
        raise MobileAuthError("email_unavailable", 503)
    email_lock(db, email)
    user = db.scalar(select(User).where(User.email == email))
    code = f"{secrets.randbelow(10**10):010d}"
    registration = db.scalar(select(MobileRegistration).where(MobileRegistration.email == email))
    body = "If you already have an EMS account, sign in or request a password reset."
    if not user:
        body = f"Your EMS verification code is {code}. It expires in 15 minutes. If you did not request this, ignore this email."
    # Send before mutating the record: failed SMTP must not invalidate a live code.
    send_email(email, body)
    if not user:
        if registration is None:
            registration = MobileRegistration(email=email)
        registration.code_hash = hash_token(code)
        registration.expires_at = utcnow() + timedelta(minutes=15)
        registration.attempts = 0
        db.add(registration)


def verify_signup(db: Session, data: VerifySignupInput) -> MobileTokens:
    email_lock(db, data.email)
    record = db.scalar(
        select(MobileRegistration).where(MobileRegistration.email == data.email).with_for_update()
    )
    if not record or record.expires_at <= utcnow() or record.attempts >= 5:
        raise MobileAuthError("invalid_code", 400)
    record.attempts += 1
    if not constant_time_eq(record.code_hash, hash_token(data.code)) or db.scalar(
        select(User.id).where(User.email == data.email)
    ):
        raise MobileAuthError("invalid_code", 400)
    user = User(
        email=data.email,
        full_name=data.full_name.strip(),
        password_hash=hash_password(data.password),
    )
    db.add(user)
    db.flush()
    pending_invite = db.scalar(
        select(Invitation.id)
        .where(
            Invitation.email == data.email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
        .limit(1)
    )
    if not pending_invite:
        organization = Organization(
            name=f"{user.full_name}'s home"[:200], slug=f"home-{uuid4().hex}"
        )
        db.add(organization)
        db.flush()
        db.add(
            Membership(user_id=user.id, organization_id=organization.id, role="organization_admin")
        )
    db.delete(record)
    return create_session(db, user, data)


def request_reset(db: Session, email: str) -> None:
    if not email_available():
        raise MobileAuthError("email_unavailable", 503)
    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    raw = generate_opaque_token()
    body = "An EMS password reset was requested. If you have not registered, create an account in the app."
    if user:
        body = f"Reset your EMS password: {get_settings().base_url}/reset-password?token={raw}\nIf you did not request this, ignore this email."
    send_email(email, body)
    if user:
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw),
                expires_at=utcnow() + timedelta(minutes=15),
            )
        )
