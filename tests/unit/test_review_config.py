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
