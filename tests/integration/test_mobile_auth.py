import re
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import hash_password, hash_token, utcnow
from app.models.mobile_auth import MobileRefreshToken, MobileRegistration, MobileSession
from app.models.organization import Membership
from app.models.user import User
from app.services import auth_service
from app.services import mobile_auth_service as service

ROOT = "/api/v1/mobile/auth"
PASSWORD = "a long test password"


@pytest.fixture(autouse=True)
def rate_limit(monkeypatch):
    monkeypatch.setattr("app.api.mobile_auth.check_fixed_window", lambda *a, **kw: 1)


@pytest.fixture()
def user(db):
    record = User(
        email=f"{uuid4().hex}@example.com",
        full_name="Alex Test",
        password_hash=hash_password(PASSWORD),
    )
    db.add(record)
    db.flush()
    return record


def installation():
    return {"installation_id": str(uuid4()), "installation_key": "k" * 43}


def login(client, user, device=None):
    device = device or installation()
    response = client.post(
        f"{ROOT}/login",
        json={
            **device,
            "email": user.email,
            "password": PASSWORD,
            "device_name": "Test phone",
            "platform": "ios",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    return device, response.json()


def headers(device, tokens):
    return {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Installation-Id": device["installation_id"],
        "X-Installation-Key": device["installation_key"],
    }


def test_rotation_replay_revokes_successor_and_hashes_only(client, db, user):
    device, tokens = login(client, user)
    session = db.get(MobileSession, tokens["session"]["id"])
    assert session.access_hash == hash_token(tokens["access_token"])
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 200
    payload = {**device, "refresh_token": tokens["refresh_token"]}
    rotated = client.post(f"{ROOT}/refresh", json=payload)
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 401
    assert client.post(f"{ROOT}/refresh", json=payload).status_code == 401
    assert client.get(f"{ROOT}/me", headers=headers(device, rotated.json())).status_code == 401
    assert db.scalar(
        select(MobileRefreshToken).where(
            MobileRefreshToken.token_hash == hash_token(tokens["refresh_token"])
        )
    ).used_at


def test_stolen_tokens_require_installation_secret(client, user):
    device, tokens = login(client, user)
    attacker = {**device, "installation_key": "x" * 43}
    assert client.get(f"{ROOT}/me", headers=headers(attacker, tokens)).status_code == 401
    assert (
        client.post(
            f"{ROOT}/refresh", json={**attacker, "refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 200
    assert (
        client.get(f"{ROOT}/me", params={"access_token": tokens["access_token"]}).status_code == 401
    )


def test_expiry_clock_skew_and_disabled_account(client, db, user):
    device, tokens = login(client, user)
    session = db.get(MobileSession, tokens["session"]["id"])
    session.access_expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 401
    refreshed = client.post(
        f"{ROOT}/refresh", json={**device, "refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200  # No client clock or client expiry participates.
    user.is_active = False
    db.flush()
    assert client.get(f"{ROOT}/me", headers=headers(device, refreshed.json())).status_code == 401
    assert (
        client.post(
            f"{ROOT}/refresh", json={**device, "refresh_token": refreshed.json()["refresh_token"]}
        ).status_code
        == 401
    )


def test_reinstall_logout_and_other_user_cannot_revoke(client, db, user):
    device, tokens = login(client, user)
    new_device, new_tokens = login(client, user)
    assert (
        client.post(
            f"{ROOT}/refresh", json={**new_device, "refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    listed = client.get(f"{ROOT}/sessions", headers=headers(new_device, new_tokens)).json()
    assert len(listed) == 2
    assert sum(s["current"] for s in listed) == 1
    other = User(
        email=f"{uuid4().hex}@example.com", full_name="Other", password_hash=hash_password(PASSWORD)
    )
    db.add(other)
    db.flush()
    foreign_device, foreign = login(client, other)
    path = f"{ROOT}/sessions/{tokens['session']['id']}/revoke"
    assert (
        client.post(
            path, headers=headers(foreign_device, foreign), json={"password": PASSWORD}
        ).status_code
        == 404
    )
    assert (
        client.post(
            path, headers=headers(new_device, new_tokens), json={"password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            path, headers=headers(new_device, new_tokens), json={"password": PASSWORD}
        ).status_code
        == 200
    )
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 401
    assert (
        client.post(
            f"{ROOT}/logout", json={**new_device, "refresh_token": new_tokens["refresh_token"]}
        ).status_code
        == 200
    )
    assert client.get(f"{ROOT}/me", headers=headers(new_device, new_tokens)).status_code == 401


def test_web_reset_and_offboarding_revoke_mobile(client, db, user):
    device, tokens = login(client, user)
    assert auth_service.revoke_all_sessions_for_user(db, user.id) >= 1
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 401


def test_generic_credentials_and_validation_do_not_echo_secrets(client, user):
    body = {
        **installation(),
        "email": user.email,
        "password": "wrong",
        "device_name": "Phone",
        "platform": "ios",
    }
    known = client.post(f"{ROOT}/login", json=body)
    missing = client.post(f"{ROOT}/login", json={**body, "email": "nobody@example.com"})
    assert known.status_code == missing.status_code == 401
    assert known.json() == missing.json() == {"code": "invalid_credentials"}
    bad = client.post(f"{ROOT}/login", json={**body, "installation_key": "sensitive-input"})
    assert bad.status_code == 422 and "sensitive-input" not in bad.text


def test_unavailable_email_is_honest_and_enumeration_safe(client, user, monkeypatch):
    monkeypatch.setattr(service, "email_available", lambda: False)
    for action in ("signup", "password-reset"):
        for email in (user.email, "unknown@example.com"):
            response = client.post(f"{ROOT}/{action}", json={"email": email})
            assert response.status_code == 503
            assert response.json() == {"code": "email_unavailable"}


def test_signup_verifies_email_creates_one_personal_org_and_rejects_reuse(client, db, monkeypatch):
    mail = []
    monkeypatch.setattr(service, "email_available", lambda: True)
    monkeypatch.setattr(service, "send_email", lambda email, body: mail.append(body))
    email = f"{uuid4().hex}@example.com"
    assert client.post(f"{ROOT}/signup", json={"email": email}).status_code == 202
    assert db.scalar(select(User).where(User.email == email)) is None
    code = re.search(r"\b\d{10}\b", mail[-1]).group()
    body = {
        **installation(),
        "email": email,
        "password": PASSWORD,
        "device_name": "Phone",
        "platform": "android",
        "full_name": "Alex",
        "code": code,
    }
    response = client.post(f"{ROOT}/signup/verify", json=body)
    assert response.status_code == 200, response.text
    assert (
        len(
            db.scalars(
                select(Membership).where(Membership.user_id == response.json()["user"]["id"])
            ).all()
        )
        == 1
    )
    assert client.post(f"{ROOT}/signup/verify", json=body).status_code == 400


def test_verification_attempts_are_durable_and_bounded(client, db, monkeypatch):
    monkeypatch.setattr(service, "email_available", lambda: True)
    monkeypatch.setattr(service, "send_email", lambda *a: None)
    email = f"{uuid4().hex}@example.com"
    client.post(f"{ROOT}/signup", json={"email": email})
    record = db.scalar(select(MobileRegistration).where(MobileRegistration.email == email))
    record.code_hash = hash_token("1234567890")
    db.flush()
    body = {
        **installation(),
        "email": email,
        "password": PASSWORD,
        "device_name": "Phone",
        "platform": "ios",
        "full_name": "Alex",
        "code": "0000000000",
    }
    for _ in range(5):
        assert client.post(f"{ROOT}/signup/verify", json=body).status_code == 400
    assert (
        client.post(f"{ROOT}/signup/verify", json={**body, "code": "1234567890"}).status_code == 400
    )
    assert record.attempts == 5


def test_rate_limit_and_redis_failure_are_closed(client, monkeypatch):
    import redis

    from app.core.rate_limit import RateLimitExceeded

    def over(*a, **kw):
        raise RateLimitExceeded(42)

    monkeypatch.setattr("app.api.mobile_auth.check_fixed_window", over)
    response = client.post(f"{ROOT}/signup", json={"email": "person@example.com"})
    assert response.status_code == 429 and response.headers["retry-after"] == "42"

    def unavailable(*a, **kw):
        raise redis.ConnectionError()

    monkeypatch.setattr("app.api.mobile_auth.check_fixed_window", unavailable)
    assert client.post(f"{ROOT}/signup", json={"email": "person@example.com"}).status_code == 503


def test_simultaneous_refresh_serializes_and_revokes_family(engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import Session

    from app.schemas.mobile_auth import Installation, LoginInput

    data = LoginInput(
        **installation(),
        email=f"{uuid4().hex}@example.com",
        password=PASSWORD,
        device_name="Race test",
        platform="ios",
    )
    with Session(engine) as setup:
        user = User(email=data.email, full_name="Race", password_hash=hash_password(PASSWORD))
        setup.add(user)
        setup.flush()
        tokens = service.create_session(setup, user, data)
        user_id, session_id = user.id, tokens.session.id
        setup.commit()
    barrier = Barrier(2)

    def rotate():
        with Session(engine) as worker:
            barrier.wait(timeout=5)
            try:
                result = service.refresh_session(
                    worker,
                    tokens.refresh_token,
                    Installation(
                        installation_id=data.installation_id, installation_key=data.installation_key
                    ),
                )
                worker.commit()
                return result is not None
            except service.MobileAuthError:
                worker.commit()
                return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(rotate) for _ in range(2)]
            assert sorted(future.result(timeout=10) for future in futures) == [False, True]
        with Session(engine) as check:
            assert check.get(MobileSession, session_id).revoked_at is not None
    finally:
        with Session(engine) as cleanup:
            cleanup.delete(cleanup.get(User, user_id))
            cleanup.commit()


def test_browser_can_revoke_mobile_only_with_password_and_csrf(client, db, user):
    from tests.web_helpers import get_csrf

    device, tokens = login(client, user)
    _, cookie = auth_service.create_session(db, user, "127.0.0.1", "Browser test")
    db.flush()
    client.cookies.set("ems_session", cookie)
    page = client.get("/settings/mobile-sessions")
    assert page.status_code == 200 and "Test phone" in page.text
    assert page.headers["cache-control"] == "no-store"
    path = f"/settings/mobile-sessions/{tokens['session']['id']}/revoke"
    assert client.post(path, data={"password": PASSWORD}).status_code == 403
    csrf = get_csrf(client)
    assert client.post(path, data={"password": "incorrect", "csrf_token": csrf}).status_code == 401
    assert (
        client.post(
            path, data={"password": PASSWORD, "csrf_token": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 401


def test_password_reset_revokes_mobile_and_cannot_be_reused(client, db, user):
    from app.models.user import PasswordResetToken

    device, tokens = login(client, user)
    raw = "a" * 43
    db.add(
        PasswordResetToken(
            user_id=user.id, token_hash=hash_token(raw), expires_at=utcnow() + timedelta(minutes=15)
        )
    )
    db.flush()
    auth_service.reset_password(db, raw, "a different long password")
    assert client.get(f"{ROOT}/me", headers=headers(device, tokens)).status_code == 401
    with pytest.raises(auth_service.AuthError):
        auth_service.reset_password(db, raw, PASSWORD)


def test_pending_invitee_signup_does_not_create_personal_org(client, db, user, monkeypatch):
    from app.models.organization import Organization
    from app.models.user import Invitation

    mail = []
    monkeypatch.setattr(service, "email_available", lambda: True)
    monkeypatch.setattr(service, "send_email", lambda email, body: mail.append(body))
    email = f"{uuid4().hex}@example.com"
    org = Organization(name="Inviting household", slug=uuid4().hex)
    db.add(org)
    db.flush()
    db.add(
        Invitation(
            organization_id=org.id,
            email=email,
            role="viewer",
            token_hash=hash_token("invite"),
            invited_by_user_id=user.id,
            expires_at=utcnow() + timedelta(days=1),
        )
    )
    db.flush()
    client.post(f"{ROOT}/signup", json={"email": email})
    code = re.search(r"\b\d{10}\b", mail[-1]).group()
    result = client.post(
        f"{ROOT}/signup/verify",
        json={
            **installation(),
            "email": email,
            "password": PASSWORD,
            "device_name": "Phone",
            "platform": "ios",
            "full_name": "Invitee",
            "code": code,
        },
    )
    assert result.status_code == 200
    assert (
        db.scalar(select(Membership).where(Membership.user_id == result.json()["user"]["id"]))
        is None
    )


def test_retention_deletes_only_expired_auth_families(client, db, user):
    _, tokens = login(client, user)
    session = db.get(MobileSession, tokens["session"]["id"])
    session.expires_at = utcnow() - timedelta(seconds=1)
    expired = MobileRegistration(
        email=f"expired-{uuid4().hex}@example.com",
        code_hash=hash_token("expired"),
        expires_at=utcnow() - timedelta(seconds=1),
        attempts=0,
    )
    active = MobileRegistration(
        email=f"active-{uuid4().hex}@example.com",
        code_hash=hash_token("active"),
        expires_at=utcnow() + timedelta(minutes=15),
        attempts=0,
    )
    db.add_all([expired, active])
    db.flush()

    result = service.purge_expired(db)
    db.flush()

    assert result == {"sessions": 1, "registrations": 1}
    assert db.get(MobileSession, session.id) is None
    assert db.get(MobileRegistration, expired.id) is None
    assert db.get(MobileRegistration, active.id) is active
