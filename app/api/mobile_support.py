"""Human authorization, typed errors and private conditional reads for mobile."""

import hashlib
from datetime import UTC
from email.utils import format_datetime
from uuid import UUID

import redis
from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import APIKeyCookie
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import StationAccess, get_current_user
from app.config import get_settings
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.database import get_db
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import User
from app.schemas.mobile import MobileError, MobileIssue

session_cookie = APIKeyCookie(name=get_settings().session_cookie_name, auto_error=False)


def fail(status, code, message, **values):
    raise HTTPException(status, MobileIssue(code=code, message=message, **values))


class MobileRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                error = MobileIssue(
                    code="invalid_request", message="Parametrii cererii nu sunt valizi."
                )
                return JSONResponse(
                    MobileError(error=error).model_dump(mode="json"),
                    422,
                    headers={"Cache-Control": "no-store"},
                )
            except HTTPException as exc:
                error = exc.detail
                if not isinstance(error, MobileIssue):
                    code = {
                        401: "unauthenticated",
                        403: "forbidden",
                        404: "no_station",
                        429: "rate_limited",
                    }.get(exc.status_code, "invalid_request")
                    if exc.status_code == 401 and request.cookies.get(
                        get_settings().session_cookie_name
                    ):
                        code = "reauth_required"
                    error = MobileIssue(code=code, message=str(exc.detail))
                return JSONResponse(
                    MobileError(error=error).model_dump(mode="json"),
                    exc.status_code,
                    headers={"Cache-Control": "no-store", **(exc.headers or {})},
                )

        return handler


def access(
    request: Request,
    station_id: UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _cookie=Depends(session_cookie),
):
    if station_id is None:
        query = (
            select(Station.id)
            .join(Organization)
            .where(Station.is_active.is_(True), Organization.status != "archived")
        )
        if not user.is_platform_admin:
            query = query.join(
                Membership, Membership.organization_id == Station.organization_id
            ).where(Membership.user_id == user.id, Membership.is_active.is_(True))
        choices = db.scalars(query.order_by(Station.id).limit(2)).all()
        if not choices:
            fail(404, "no_station", "Nu exista o statie accesibila.")
        if len(choices) > 1:
            fail(409, "station_required", "Selecteaza explicit statia.")
        station_id = choices[0]
    station, role = StationAccess()(station_id=station_id, db=db, user=user)
    try:
        check_fixed_window(f"mobile_read:{user.id}", 120, 60)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            MobileIssue(
                code="rate_limited",
                message="Prea multe cereri.",
                retry_after_seconds=exc.retry_after_seconds,
            ),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from None
    except redis.RedisError:
        fail(503, "service_unavailable", "Serviciul este temporar indisponibil.")
    return station, role, user


def private_response(request, model, *, modified=None, conditional=False):
    body = model.model_dump_json().encode()
    headers = {
        "Cache-Control": "private, no-cache, max-age=0, must-revalidate"
        if conditional
        else "no-store",
        "Vary": "Cookie, Authorization",
    }
    if conditional:
        # Include user identity even when two authorized users have identical data.
        identity = str(request.state.auth_user.id).encode()
        etag = '"' + hashlib.sha256(identity + b":" + body).hexdigest() + '"'
        headers["ETag"] = etag
        if modified:
            headers["Last-Modified"] = format_datetime(modified.astimezone(UTC), usegmt=True)
        incoming = request.headers.get("if-none-match", "")
        if any(tag.strip().removeprefix("W/") in (etag, "*") for tag in incoming.split(",")):
            return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)
