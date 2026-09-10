"""Autentificare si limitare pentru API-ul de dispozitive.

Schema: `Authorization: Bearer <device_id>.<secret>`. Secretul este verificat
Argon2 impotriva credentialei active a dispozitivului. Fiecare cerere este
supusa rate limiting per dispozitiv si unei limite de dimensiune a payload-ului.
"""
from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.rate_limit import RateLimitExceeded, check_fixed_window
from app.core.security import utcnow, verify_password
from app.database import get_db
from app.models.device import Device, DeviceCredential
from app.models.enums import DeviceStatus
from app.services import device_service

settings = get_settings()


def enforce_payload_limit(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.device_max_payload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload prea mare (max {settings.device_max_payload_bytes} bytes).",
        )


def get_authenticated_device(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    _payload_ok: None = Depends(enforce_payload_limit),
) -> Device:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Header Authorization: Bearer <device_id>.<secret> lipseste.")

    raw = authorization.removeprefix("Bearer ").strip()
    if "." not in raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Format de credentiale invalid.")

    device_id_str, secret = raw.split(".", 1)
    try:
        device_id = uuid.UUID(device_id_str)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="device_id invalid.") from exc

    try:
        check_fixed_window(
            f"device_rl:{device_id}", settings.device_api_rate_limit_per_minute, 60
        )
    except RateLimitExceeded as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Prea multe cereri.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    device = db.get(Device, device_id)
    if device is None or device.status != DeviceStatus.active.value:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Dispozitiv inexistent sau inactiv.")

    credentials = db.scalars(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device.id,
            DeviceCredential.is_active.is_(True),
            DeviceCredential.revoked_at.is_(None),
        )
    ).all()

    matched = None
    for cred in credentials:
        if verify_password(secret, cred.secret_hash):
            matched = cred
            break

    if matched is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Credentiale invalide.")

    matched.last_used_at = utcnow()
    db.add(matched)

    # Issue #16: la prima cerere autentificata reusita cu o credentiala emisa
    # prin alocare de enrollment automat, stergem copia in clar pastrata
    # temporar pentru recuperare idempotenta (vezi device_service.
    # mark_bootstrap_credential_delivered si comentariul de pe
    # Device.pending_credential_secret) -- fereastra de expunere se inchide
    # imediat ce dispozitivul a demonstrat ca a primit-o.
    if device.pending_credential_secret is not None:
        device_service.mark_bootstrap_credential_delivered(db, device)

    db.flush()

    request.state.device_credential_id = matched.id
    return device
