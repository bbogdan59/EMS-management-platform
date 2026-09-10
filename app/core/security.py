"""Primitive de securitate: hashing parole (Argon2), token-uri opace pentru
sesiuni/invitatii/reset parola, semnare CSRF. Nu se logheaza niciodata
parole, token-uri sau secrete in clar (nici in loguri, nici in audit)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError


_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def generate_opaque_token(nbytes: int = 32) -> str:
    """Token aleator, sigur criptografic, folosit pentru sesiuni/invitatii/reset.
    Doar hash-ul (SHA-256) se stocheaza in baza de date."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def expires_in(minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
    return utcnow() + timedelta(minutes=minutes, hours=hours, days=days)


def generate_claim_code() -> tuple[str, str]:
    """Genereaza un cod de asociere lizibil (ex. EMS-7F3K-9QRT) si returneaza
    (cod_complet, prefix_afisabil_in_ui)."""
    raw = secrets.token_hex(5).upper()
    code = f"EMS-{raw[:4]}-{raw[4:8]}"
    return code, code[:8]
