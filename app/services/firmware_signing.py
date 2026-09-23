"""Ed25519 signing for published firmware releases (issue #168). The
PRIVATE key lives only in `Settings.firmware_signing_private_key_pem` (an
environment variable) -- never persisted in the DB and never logged.
Verification of the signature is the DEVICE's own job
(EMS-device-code#13); this module only ever produces a signature, at
publish time, server-side."""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class FirmwareSigningError(Exception):
    pass


def sign(data: bytes, *, private_key_pem: str | None) -> bytes:
    if not private_key_pem:
        raise FirmwareSigningError(
            "FIRMWARE_SIGNING_PRIVATE_KEY_PEM nu este configurata -- un release nu poate fi "
            "publicat fara semnatura verificabila."
        )
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except ValueError as exc:
        raise FirmwareSigningError("Cheia de semnare configurata nu este o cheie PEM valida.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise FirmwareSigningError("Cheia de semnare configurata trebuie sa fie Ed25519.")
    return key.sign(data)


def sign_hex(data: bytes, *, private_key_pem: str | None) -> str:
    return sign(data, private_key_pem=private_key_pem).hex()
