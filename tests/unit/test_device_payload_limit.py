"""Regresii pentru issue #10: limita reala a corpului HTTP al API-ului de
dispozitive. `Content-Length` e doar o DECLARATIE a clientului -- testam
direct dependency-ul FastAPI cu un scope/receive ASGI fabricat, ca sa
verificam ca limita se aplica pe bytes-ii REAL cititi din stream, nu doar
pe header, si ca un header invalid nu produce un 500 brut.

Corutina ruleaza intr-un thread nou, cu propriul event loop `asyncio.run()`
izolat, in loc sa ne bazam pe modul auto al pytest-asyncio sau pe
`asyncio.run()` direct in thread-ul principal de test: in suita completa,
alte teste care folosesc TestClient-ul bazat pe httpx/anyio lasa un event
loop activ in thread-ul principal, ceea ce face fie ca `async def test_...`
simplu sa nu mai fie asteptat de plugin, fie ca `asyncio.run()` direct sa
esueze cu "cannot be called from a running event loop". Un thread nou nu are
niciun loop ambiant, deci comportamentul e deterministic indiferent de ordinea
sau restul suitei."""
from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1.device_deps import enforce_payload_limit
from app.config import get_settings

settings = get_settings()


def _request(headers: list[tuple[bytes, bytes]], body: bytes) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/v1/telemetry/batch", "headers": headers}
    return Request(scope, receive)


def _run(request: Request) -> None:
    outcome: dict[str, BaseException] = {}

    def worker():
        try:
            asyncio.run(enforce_payload_limit(request))
        except BaseException as exc:  # repropagat neschimbat mai jos, inclusiv HTTPException
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]


def test_oversized_body_rejected_even_without_content_length_header():
    """Un client care omite header-ul Content-Length (ex. transfer chunked)
    trebuia sa treaca nedetectat prin vechea verificare (doar pe header).
    Corpul e totusi peste limita configurata -- trebuie respins la citirea
    efectiva a stream-ului, nu doar la o verificare de header absent."""
    big_body = b"x" * (settings.device_max_payload_bytes + 1024)
    request = _request(headers=[], body=big_body)

    with pytest.raises(HTTPException) as exc_info:
        _run(request)
    assert exc_info.value.status_code == 413


def test_body_within_limit_and_no_content_length_header_is_accepted():
    small_body = b"{}"
    request = _request(headers=[], body=small_body)
    _run(request)
    assert request._body == small_body


def test_malformed_content_length_header_returns_400_not_500():
    request = _request(headers=[(b"content-length", b"not-a-number")], body=b"{}")
    with pytest.raises(HTTPException) as exc_info:
        _run(request)
    assert exc_info.value.status_code == 400


def test_content_length_header_over_limit_still_rejected():
    over_limit = settings.device_max_payload_bytes + 1
    request = _request(headers=[(b"content-length", str(over_limit).encode())], body=b"x" * over_limit)
    with pytest.raises(HTTPException) as exc_info:
        _run(request)
    assert exc_info.value.status_code == 413
