from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import record_audit
from app.core.csrf import verify_csrf
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.database import get_db
from app.services import auth_service
from app.web.response_headers import apply_no_store_headers
from app.web.templating import templates

router = APIRouter()
settings = get_settings()


def _sensitive_template(request: Request, template: str, context: dict, status_code: int = 200):
    response = templates.TemplateResponse(request, template, context, status_code=status_code)
    return apply_no_store_headers(response)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "auth/login.html", {"next": next, "error": None})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    try:
        check_fixed_window(
            f"login_attempts:{ip}",
            settings.login_rate_limit_attempts,
            settings.login_rate_limit_window_seconds,
        )
    except RateLimitExceeded as exc:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "next": next,
                "error": f"Prea multe incercari de autentificare. Reincearca peste {exc.retry_after_seconds}s.",
            },
            status_code=429,
        )

    try:
        user = auth_service.authenticate(db, email, password)
    except auth_service.AccountLocked as exc:
        record_audit(db, action="login_failed_locked", resource_type="user", actor_label=email, ip_address=ip)
        db.commit()
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "next": next,
                "error": f"Cont blocat temporar din cauza incercarilor esuate. Reincearca peste {exc.retry_after_seconds}s.",
            },
            status_code=423,
        )
    except auth_service.InvalidCredentials:
        record_audit(db, action="login_failed", resource_type="user", actor_label=email, ip_address=ip)
        db.commit()
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"next": next, "error": "Email sau parola incorecte."},
            status_code=401,
        )

    _session, raw_token = auth_service.create_session(db, user, ip, request.headers.get("user-agent"))
    record_audit(
        db, action="login_succeeded", resource_type="user", resource_id=str(user.id),
        actor_user_id=user.id, actor_label=user.email, ip_address=ip,
    )
    db.commit()

    redirect_to = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
    )
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    raw_token = request.cookies.get(settings.session_cookie_name)
    if raw_token:
        from sqlalchemy import select

        from app.core.security import hash_token
        from app.models.user import Session as UserSession

        token_hash = hash_token(raw_token)
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
        if sess is not None:
            auth_service.revoke_session(db, sess)
            record_audit(db, action="logout", resource_type="user", actor_user_id=sess.user_id, actor_label="")
            db.commit()

    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.session_cookie_name)
    return response


@router.get("/request-password-reset")
def request_reset_form(request: Request):
    return templates.TemplateResponse(request, "auth/request_password_reset.html", {"sent": False})


@router.post("/request-password-reset", dependencies=[Depends(verify_csrf)])
def request_reset_submit(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    auth_service.request_password_reset(db, email)
    db.commit()
    # Daca nu exista un backend de email real (issue #149), tokenul de
    # resetare tot e creat in DB (util pentru un flux administrativ viitor),
    # dar UI-ul NU pretinde ca a trimis un email nelivrabil -- mesajul e
    # identic indiferent daca adresa exista, ca sa nu enumere conturile.
    context = {"sent": True, "email_deliverable": settings.email_deliverable}
    return templates.TemplateResponse(request, "auth/request_password_reset.html", context)


@router.get("/reset-password")
def reset_password_form(request: Request, token: str):
    return _sensitive_template(request, "auth/reset_password.html", {"token": token, "error": None})


@router.post("/reset-password", dependencies=[Depends(verify_csrf)])
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm:
        return _sensitive_template(
            request, "auth/reset_password.html", {"token": token, "error": "Parolele nu coincid."}, status_code=400
        )
    if len(password) < 10:
        return _sensitive_template(
            request,
            "auth/reset_password.html",
            {"token": token, "error": "Parola trebuie sa aiba cel putin 10 caractere."},
            status_code=400,
        )
    try:
        user = auth_service.reset_password(db, token, password)
    except auth_service.AuthError as exc:
        db.rollback()
        return _sensitive_template(
            request, "auth/reset_password.html", {"token": token, "error": str(exc)}, status_code=400
        )
    record_audit(db, action="password_reset_completed", resource_type="user", actor_user_id=user.id, actor_label=user.email)
    db.commit()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/accept-invitation")
def accept_invitation_form(request: Request, token: str):
    return _sensitive_template(request, "auth/accept_invitation.html", {"token": token, "error": None})


@router.post("/accept-invitation", dependencies=[Depends(verify_csrf)])
def accept_invitation_submit(
    request: Request,
    token: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm:
        return _sensitive_template(
            request, "auth/accept_invitation.html", {"token": token, "error": "Parolele nu coincid."}, status_code=400
        )
    if len(password) < 10:
        return _sensitive_template(
            request,
            "auth/accept_invitation.html",
            {"token": token, "error": "Parola trebuie sa aiba cel putin 10 caractere."},
            status_code=400,
        )
    try:
        user = auth_service.accept_invitation(db, token, password, full_name)
    except auth_service.AuthError as exc:
        db.rollback()
        return _sensitive_template(
            request, "auth/accept_invitation.html", {"token": token, "error": str(exc)}, status_code=400
        )
    record_audit(db, action="invitation_accepted", resource_type="user", actor_user_id=user.id, actor_label=user.email)
    db.commit()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/bootstrap-admin")
def bootstrap_admin_form(request: Request):
    return templates.TemplateResponse(request, "auth/bootstrap_admin.html", {"error": None})


@router.post("/bootstrap-admin", dependencies=[Depends(verify_csrf)])
def bootstrap_admin_submit(
    request: Request,
    bootstrap_token: str = Form(...),
    email: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = auth_service.bootstrap_first_admin(db, bootstrap_token, email, password, full_name)
    except auth_service.AuthError as exc:
        db.rollback()
        return templates.TemplateResponse(request, "auth/bootstrap_admin.html", {"error": str(exc)}, status_code=400)
    record_audit(db, action="bootstrap_admin_created", resource_type="user", actor_user_id=user.id, actor_label=user.email)
    db.commit()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
