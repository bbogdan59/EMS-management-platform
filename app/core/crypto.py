"""Criptare simetrica la repaus pentru secrete reversibile (spre deosebire de
`app.core.security`, care hash-uieste ireversibil parole/token-uri opace).

Necesar pentru integrari externe unde trebuie sa PUTEM recupera secretul in
clar ca sa-l retrimitem catre furnizor (ex. contul Deye Cloud al clientului,
folosit de worker-ul de polling) -- un hash unidirectional nu ar permite asta.
Foloseste Fernet (AES-128-CBC + HMAC, din pachetul `cryptography`, deja
dependinta a proiectului) cu o cheie derivata din `SECRET_KEY` prin HKDF, ca sa
nu introducem inca o variabila de mediu obligatorie separata -- o rotatie a
`SECRET_KEY` ar invalida orice secret criptat existent (accepta explicit,
documentat in docs/LIMITATIONS.md, nu ascuns)."""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

settings = get_settings()


class DecryptionError(Exception):
    """Secretul stocat nu poate fi decriptat cu cheia curenta (SECRET_KEY
    schimbat dupa criptare, sau date corupte) -- niciodata tratat tacit ca
    'secret lipsa', ca sa nu se confunde cu o deconectare intentionata."""


def _fernet() -> Fernet:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"ems-platform.deye-cloud.credential-encryption.v1",
        info=b"fernet-key",
    )
    derived = hkdf.derive(settings.secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError("Secretul stocat nu a putut fi decriptat.") from exc
