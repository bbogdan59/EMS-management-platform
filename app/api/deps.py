"""Dependinte FastAPI pentru autentificare si autorizare pe partea web
(cookie de sesiune). API-ul pentru dispozitive foloseste alt mecanism, vezi
app/api/v1/device_deps.py. Fiecare resursa protejata (inclusiv SSE, exporturi,
joburi, comenzi) trece prin aceste dependinte -- nu doar UI-ul ascunde butoane."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.rbac import role_at_least
from app.core.security import hash_token, utcnow
from app.database import get_db
from app.models.enums import Role
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import Session as UserSession
from app.models.user import User

settings = get_settings()


class AuthContext:
    __slots__ = ("session", "user")

    def __init__(self, user: User, session: UserSession):
        self.user = user
        self.session = session


def get_current_context(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthContext:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Autentificare necesara.")

    token_hash = hash_token(raw_token)
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if sess is None or not sess.is_valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sesiune invalida sau expirata.")

    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Cont inactiv.")

    sess.last_seen_at = utcnow()
    db.add(sess)
    db.flush()

    request.state.auth_user = user
    request.state.auth_session = sess
    return AuthContext(user=user, session=sess)


def get_current_user(ctx: AuthContext = Depends(get_current_context)) -> User:
    return ctx.user


def get_optional_user(
    request: Request, db: Session = Depends(get_db)
) -> User | None:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if sess is None or not sess.is_valid:
        return None
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Necesita rol platform_admin.")
    return user


def _check_organization_status(org: Organization, min_role: str) -> None:
    """Aplica efectul unei organizatii suspendate/arhivate asupra accesului
    non-platform_admin (issue #24) -- platform_admin trece mereu neafectat
    (trebuie sa poata gestiona/reactiva organizatia din panoul de admin).

    - `archived`: blocheaza TOT accesul (stare finala, apropiata de
      offboarding) -- inclusiv citirea.
    - `suspended`: blocheaza doar actiunile care cer mai mult decat `viewer`
      (scriere/operare) -- citirea ramane permisa, ca un client suspendat sa
      isi poata vedea/exporta in continuare datele inainte de reactivare.
    - `active`: neschimbat."""
    if org.status == "archived":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Organizatia este arhivata.")
    if org.status == "suspended" and role_at_least(min_role, Role.operator):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Organizatia este suspendata.")


class StationAccess:
    """Rezolva statia + rolul utilizatorului in organizatia ei si verifica
    apartenenta. platform_admin are acces la orice statie (pentru panoul de
    administrare), dar orice alt rol trebuie sa aiba o membership activa."""

    def __init__(self, min_role: str = "viewer"):
        self.min_role = min_role

    def __call__(
        self,
        station_id: uuid.UUID,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> tuple[Station, str]:
        station = db.get(Station, station_id)
        if station is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Statia nu exista.")

        if user.is_platform_admin:
            return station, "platform_admin"

        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == station.organization_id,
                Membership.is_active.is_(True),
            )
        )
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Nu ai acces la aceasta statie.")
        if not role_at_least(membership.role, self.min_role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Rol insuficient pentru aceasta actiune.")
        organization = db.get(Organization, station.organization_id)
        if organization is not None:
            _check_organization_status(organization, self.min_role)
        return station, membership.role


class OrganizationAccess:
    def __init__(self, min_role: str = "viewer"):
        self.min_role = min_role

    def __call__(
        self,
        organization_id: uuid.UUID,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> tuple[Organization, str]:
        org = db.get(Organization, organization_id)
        if org is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Organizatia nu exista.")
        if user.is_platform_admin:
            return org, "platform_admin"
        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization_id,
                Membership.is_active.is_(True),
            )
        )
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Nu ai acces la aceasta organizatie.")
        if not role_at_least(membership.role, self.min_role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Rol insuficient pentru aceasta actiune.")
        _check_organization_status(org, self.min_role)
        return org, membership.role


def user_organization_ids(db: Session, user: User) -> list[uuid.UUID]:
    if user.is_platform_admin:
        return [row[0] for row in db.execute(select(Organization.id)).all()]
    return [
        row[0]
        for row in db.execute(
            select(Membership.organization_id).where(
                Membership.user_id == user.id, Membership.is_active.is_(True)
            )
        ).all()
    ]
