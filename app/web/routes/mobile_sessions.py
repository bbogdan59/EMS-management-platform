"""Browser control for a lost mobile installation; cookie auth and CSRF apply."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.mobile_auth import limit
from app.core.csrf import verify_csrf
from app.core.security import utcnow
from app.database import get_db
from app.models.mobile_auth import MobileSession
from app.models.user import User
from app.services import mobile_auth_service as service
from app.web.response_headers import apply_no_store_headers
from app.web.templating import templates

router = APIRouter()


def render(request: Request, db: Session, user: User, error: str | None = None, status: int = 200):
    sessions = db.scalars(
        select(MobileSession)
        .where(
            MobileSession.user_id == user.id,
            MobileSession.revoked_at.is_(None),
            MobileSession.expires_at > utcnow(),
        )
        .order_by(MobileSession.created_at.desc())
        .limit(service.MAX_SESSIONS)
    ).all()
    return apply_no_store_headers(
        templates.TemplateResponse(
            request,
            "auth/mobile_sessions.html",
            {
                "current_user": user,
                "sessions": sessions,
                "error": error,
            },
            status_code=status,
        )
    )


@router.get("/settings/mobile-sessions")
def session_list(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return render(request, db, user)


@router.post("/settings/mobile-sessions/{session_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke(
    session_id: UUID,
    request: Request,
    password: str = Form(..., max_length=128),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        limit(request, "login", user.email)
        service.authenticate(db, user.email, password)
        session = db.scalar(
            select(MobileSession)
            .where(MobileSession.id == session_id, MobileSession.user_id == user.id)
            .with_for_update()
        )
        if session is None:
            raise service.MobileAuthError("not_found", 404)
        session.revoked_at = utcnow()
        db.commit()
    except service.MobileAuthError as exc:
        db.commit()
        return render(
            request,
            db,
            user,
            "Revocarea nu a reusit. Verifica parola sau incearca mai tarziu.",
            exc.status,
        )
    return apply_no_store_headers(RedirectResponse("/settings/mobile-sessions", status_code=303))
