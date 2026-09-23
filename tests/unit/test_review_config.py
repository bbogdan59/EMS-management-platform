import pytest

from app.config import Settings


def test_synthetic_market_prices_default_off(monkeypatch):
    monkeypatch.delenv("OPCOM_USE_SYNTHETIC_FIXTURE_ON_FAILURE", raising=False)
    assert not Settings(_env_file=None).opcom_use_synthetic_fixture_on_failure


def test_production_rejects_synthetic_market_prices():
    with pytest.raises(RuntimeError, match="OPCOM sintetice"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key="test-only-explicit-key",
            demo_mode_enabled=False,
            opcom_use_synthetic_fixture_on_failure=True,
        )


def test_production_rejects_local_dev_only_firmware_storage():
    """issue #168: firmware artifacts must never land on Railway's ephemeral
    disk -- production requires an explicit object storage backend."""
    with pytest.raises(RuntimeError, match="FIRMWARE_STORAGE_BACKEND"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key="test-only-explicit-key",
            session_cookie_secure=True,
            demo_mode_enabled=False,
            opcom_use_synthetic_fixture_on_failure=False,
            legacy_claim_code_enabled=False,
            invitation_delivery_mode="manual_link",
            firmware_storage_backend="local_dev_only",
        )


def _production_kwargs(**overrides):
    base = {
        "_env_file": None,
        "environment": "production",
        "secret_key": "test-only-explicit-key",
        "session_cookie_secure": True,
        "demo_mode_enabled": False,
        "opcom_use_synthetic_fixture_on_failure": False,
        "legacy_claim_code_enabled": False,
        # issue #168: local_dev_only is refused in production (see below).
        "firmware_storage_backend": "s3",
        "firmware_s3_bucket": "test-bucket",
    }
    base.update(overrides)
    return base


def test_production_console_email_rejected_in_default_email_mode():
    """Modul implicit 'email' cere un backend care poate livra efectiv
    (issue #149) -- consola scrie doar in loguri redactate."""
    with pytest.raises(RuntimeError, match="EMAIL_BACKEND=console"):
        Settings(**_production_kwargs(email_backend="console", invitation_delivery_mode="email"))


def test_production_console_email_allowed_with_manual_link_invitations():
    """INVITATION_DELIVERY_MODE=manual_link e exact escape hatch-ul pentru
    productie fara SMTP (issue #149): invitatiile nu mai trec deloc prin
    adaptorul de email, deci EMAIL_BACKEND=console nu mai e un blocaj."""
    settings = Settings(**_production_kwargs(email_backend="console", invitation_delivery_mode="manual_link"))
    assert settings.invitation_manual_link is True
    assert settings.email_deliverable is False


def test_production_smtp_allowed_regardless_of_invitation_delivery_mode():
    Settings(**_production_kwargs(email_backend="smtp", invitation_delivery_mode="email"))
    Settings(**_production_kwargs(email_backend="smtp", invitation_delivery_mode="manual_link"))


def test_invitation_delivery_mode_defaults_to_email():
    assert Settings(_env_file=None).invitation_delivery_mode == "email"
    assert Settings(_env_file=None).invitation_manual_link is False
