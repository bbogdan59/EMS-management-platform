from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.notification_service import delivery_time, validate_matrix, validate_subscription


@pytest.mark.parametrize(
    "at,expected",
    [
        (datetime(2026, 3, 29, 0, 30, tzinfo=UTC), datetime(2026, 3, 29, 5, tzinfo=UTC)),
        (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 6, tzinfo=UTC)),
        (datetime(2024, 2, 29, 0, 30, tzinfo=UTC), datetime(2024, 2, 29, 6, tzinfo=UTC)),
    ],
)
def test_quiet_hours_use_real_utc_instants_across_dst_and_leap_day(at, expected):
    pref = SimpleNamespace(timezone="Europe/Bucharest", quiet_start=22, quiet_end=8)
    assert delivery_time(pref, at, "immediate") == expected
    assert delivery_time(pref, at, "immediate", critical=True) == at
    assert delivery_time(pref, at, "daily") == expected


def test_daily_digest_after_morning_uses_next_day():
    pref = SimpleNamespace(timezone="Europe/Bucharest", quiet_start=22, quiet_end=8)
    at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert delivery_time(pref, at, "daily") == datetime(2026, 9, 2, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "url",
    [
        "http://fcm.googleapis.com/push",
        "https://127.0.0.1/push",
        "https://fcm.googleapis.com.evil.invalid/push",
        "https://user:secret@fcm.googleapis.com/push",
        "https://fcm.googleapis.com:8443/push",
        "https://web.push.apple.com/push#secret",
    ],
)
def test_push_endpoint_allowlist_rejects_ssrf_and_credentials(url):
    with pytest.raises(ValueError):
        validate_subscription({"endpoint": url, "keys": {}})


def test_preferences_reject_unknown_categories_and_modes():
    with pytest.raises(ValueError):
        validate_matrix({"warning:warning:email": "run_command"})
    with pytest.raises(ValueError):
        validate_matrix({"anything": "daily"})


def test_push_adapter_uses_vapid_timeout_and_no_redirects(monkeypatch):
    import base64
    import json
    from types import SimpleNamespace

    import pywebpush
    import requests

    from app.config import get_settings
    from app.services.notification_service import PushAdapter

    subscription = {
        "endpoint": "https://fcm.googleapis.com/push/example",
        "keys": {
            "auth": base64.urlsafe_b64encode(b"a" * 16).decode(),
            "p256dh": base64.urlsafe_b64encode(b"b" * 65).decode(),
        },
    }
    settings = get_settings()
    monkeypatch.setattr(settings, "notifications_vapid_private_key", "test-private-key")
    monkeypatch.setattr(settings, "notifications_vapid_subject", "mailto:test@example.com")
    calls = []

    def send(session, *args, **kwargs):
        assert kwargs["allow_redirects"] is False
        return SimpleNamespace(status_code=201)

    def webpush(info, data, **kwargs):
        assert info == subscription
        assert json.loads(data)["url"] == "/notifications"
        assert kwargs["timeout"] == 10
        assert kwargs["vapid_claims"] == {"sub": "mailto:test@example.com"}
        calls.append(kwargs["headers"]["Topic"])
        return kwargs["requests_session"].post(info["endpoint"])

    monkeypatch.setattr(requests.Session, "post", send)
    monkeypatch.setattr(pywebpush, "webpush", webpush)
    for _ in range(2):
        PushAdapter().send(subscription, {"url": "/notifications"}, "stable-delivery-id")
    assert calls[0] == calls[1]
