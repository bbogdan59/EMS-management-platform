from typing import Annotated
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, Header, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.core.security import hash_token, utcnow
from app.database import get_db
from app.models.mobile_auth import MobileSession
from app.schemas.mobile_auth import (
    EmailInput,
    Installation,
    LoginInput,
    MobileAccepted,
    MobileCapabilities,
    MobileFailure,
    MobileIdentity,
    MobileSessionInfo,
    MobileTokens,
    RefreshInput,
    RevokeInput,
    VerifySignupInput,
)
from app.services import mobile_auth_service as service

router = APIRouter(
    prefix="/api/v1/mobile/auth",
    tags=["mobile-auth"],
    responses={code: {"model": MobileFailure} for code in (400, 401, 404, 422, 429, 503)},
)


def transaction(db: Session = Depends(get_db)):
    try:
        yield db
        db.commit()
    except service.MobileAuthError:
        # Persist failed-attempt counters and replay revocation on error responses.
        db.commit()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise service.MobileAuthError("invalid_request", 400) from exc
    except Exception:
        db.rollback()
        raise


DB = Annotated[Session, Depends(transaction, scope="function")]


def limit(request: Request, action: str, email: str | None = None):
    ip = request.client.host if request.client else "unknown"
    try:
        check_fixed_window(
            f"mobile:{action}:ip:{hash_token(ip)}", 60 if action == "refresh" else 20, 300
        )
        if email:
            check_fixed_window(f"mobile:{action}:email:{hash_token(email)}", 8, 300)
    except RateLimitExceeded as exc:
        raise service.MobileAuthError("rate_limited", 429, exc.retry_after_seconds) from exc
    except redis.RedisError as exc:
        raise service.MobileAuthError("unavailable", 503) from exc


def current(
    db: DB,
    authorization: Annotated[str | None, Header()] = None,
    x_installation_id: Annotated[str | None, Header()] = None,
    x_installation_key: Annotated[str | None, Header()] = None,
):
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 150:
        raise service.MobileAuthError("invalid_session")
    try:
        installation = Installation(
            installation_id=x_installation_id, installation_key=x_installation_key
        )
    except ValidationError as exc:
        raise service.MobileAuthError("invalid_session") from exc
    return service.authorize(db, authorization[7:], installation)


@router.get("/capabilities", response_model=MobileCapabilities)
def capabilities():
    return MobileCapabilities(email_delivery_available=service.email_available())


@router.post("/login", response_model=MobileTokens)
def login(data: LoginInput, request: Request, db: DB):
    limit(request, "login", data.email)
    return service.create_session(db, service.authenticate(db, data.email, data.password), data)


@router.post("/signup", response_model=MobileAccepted, status_code=202)
def signup(data: EmailInput, request: Request, db: DB):
    limit(request, "email", data.email)
    service.request_signup(db, data.email)
    return MobileAccepted()


@router.post("/signup/verify", response_model=MobileTokens)
def verify(data: VerifySignupInput, request: Request, db: DB):
    limit(request, "verify", data.email)
    return service.verify_signup(db, data)


@router.post("/password-reset", response_model=MobileAccepted, status_code=202)
def password_reset(data: EmailInput, request: Request, db: DB):
    limit(request, "email", data.email)
    service.request_reset(db, data.email)
    return MobileAccepted()


@router.post("/refresh", response_model=MobileTokens)
def refresh(data: RefreshInput, request: Request, db: DB):
    limit(request, "refresh")
    return service.refresh_session(db, data.refresh_token, data)


@router.post("/logout", response_model=MobileAccepted)
def logout(data: RefreshInput, request: Request, db: DB):
    limit(request, "refresh")
    service.refresh_session(db, data.refresh_token, data, logout=True)
    return MobileAccepted()


@router.get("/me", response_model=MobileIdentity)
def me(context=Depends(current)):
    return service.identity(*context)


@router.get("/sessions", response_model=list[MobileSessionInfo])
def sessions(db: DB, context=Depends(current)):
    user, session = context
    records = db.scalars(
        select(MobileSession)
        .where(
            MobileSession.user_id == user.id,
            MobileSession.revoked_at.is_(None),
            MobileSession.expires_at > utcnow(),
        )
        .order_by(MobileSession.created_at.desc())
        .limit(service.MAX_SESSIONS)
    ).all()
    return [service.session_info(record, session.id) for record in records]


@router.post("/sessions/{session_id}/revoke", response_model=MobileAccepted)
def revoke(session_id: UUID, data: RevokeInput, request: Request, db: DB, context=Depends(current)):
    user, _ = context
    limit(request, "login", user.email)
    service.authenticate(db, user.email, data.password)
    record = db.scalar(
        select(MobileSession)
        .where(MobileSession.id == session_id, MobileSession.user_id == user.id)
        .with_for_update()
    )
    if record is None:
        raise service.MobileAuthError("not_found", 404)
    record.revoked_at = utcnow()
    return MobileAccepted()
