"""Protectie CSRF prin cookie cu verificare dubla (double-submit cookie).

Un middleware (app.main) garanteaza ca fiecare raspuns seteaza cookie-ul
`csrf_cookie_name` daca lipseste. Formularele (Jinja) il citesc prin
`csrf_token(request)` si il trimit atat ca field ascuns cat si -- pentru
cereri HTMX -- ca header `X-CSRF-Token`. La orice cerere de mutatie
(POST/PUT/PATCH/DELETE) valoarea trimisa trebuie sa coincida cu cookie-ul."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.core.security import constant_time_eq, generate_opaque_token

settings = get_settings()

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_csrf_cookie(request: Request) -> str | None:
    return request.cookies.get(settings.csrf_cookie_name)


def prepare_csrf_token(request: Request) -> str | None:
    """Ruleaza INAINTE de handler: daca cererea nu are inca cookie-ul CSRF,
    genereaza tokenul acum si il pune in request.state, ca orice template
    randat in cadrul acestei cereri (inclusiv prima vizita a unui utilizator)
    sa foloseasca aceeasi valoare care va fi trimisa si ca si cookie.
    Returneaza tokenul nou daca a fost generat unul, altfel None."""
    if get_csrf_cookie(request):
        return None
    token = generate_opaque_token(24)
    request.state._csrf_token_for_render = token
    return token


def finalize_csrf_cookie(request: Request, response, pending_token: str | None) -> None:
    if pending_token:
        response.set_cookie(
            settings.csrf_cookie_name,
            pending_token,
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )


def csrf_token_for_template(request: Request) -> str:
    return getattr(request.state, "_csrf_token_for_render", None) or get_csrf_cookie(request) or ""


async def verify_csrf(request: Request) -> None:
    if request.method not in MUTATING_METHODS:
        return
    cookie_token = get_csrf_cookie(request)
    if not cookie_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Token CSRF lipsa.")

    header_token = request.headers.get("X-CSRF-Token")
    if header_token:
        submitted = header_token
    else:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            submitted = None
        else:
            form = await request.form()
            submitted = form.get("csrf_token")

    if not submitted or not constant_time_eq(submitted, cookie_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Token CSRF invalid.")
