import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import firmware_signing


def _test_keypair_pem() -> tuple[str, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key


def test_sign_produces_a_verifiable_ed25519_signature():
    pem, key = _test_keypair_pem()
    data = b"release artifact bytes"
    signature = firmware_signing.sign(data, private_key_pem=pem)
    key.public_key().verify(signature, data)  # raises InvalidSignature if wrong


def test_sign_hex_returns_hex_encoded_signature():
    pem, _key = _test_keypair_pem()
    signature_hex = firmware_signing.sign_hex(b"data", private_key_pem=pem)
    assert len(signature_hex) == 128  # 64-byte Ed25519 signature, hex-encoded
    bytes.fromhex(signature_hex)  # must not raise


def test_sign_requires_a_configured_key():
    with pytest.raises(firmware_signing.FirmwareSigningError, match="nu este configurata"):
        firmware_signing.sign(b"data", private_key_pem=None)


def test_sign_rejects_invalid_pem():
    with pytest.raises(firmware_signing.FirmwareSigningError, match="nu este o cheie PEM"):
        firmware_signing.sign(b"data", private_key_pem="not a real pem")


def test_sign_rejects_non_ed25519_key():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    with pytest.raises(firmware_signing.FirmwareSigningError, match="Ed25519"):
        firmware_signing.sign(b"data", private_key_pem=pem)
