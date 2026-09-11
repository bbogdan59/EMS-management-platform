from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.rate_limit import reset_key
from app.models.organization import Membership
from app.models.user import Invitation
from app.services import auth_service
from app.web.routes import sse
from tests.factories import make_membership, make_org, make_station, make_user
from tests.web_helpers import login


def test_organization_invitation_rejects_global_role(client, db):
    reset_key("login_attempts:testclient")
    manager = make_user(db, email="manager-security@test.local")
    organization = make_org(db, "Security Org")
    make_membership(db, manager, organization, role="organization_admin")
    db.commit()
    assert login(client, manager.email, "TestPass1234").status_code == 303

    response = client.post(
        f"/organizations/{organization.id}/invitations",
        data={
            "csrf_token": client.cookies["ems_csrf"],
            "email": "target@test.local",
            "role": "platform_admin",
        },
    )

    assert response.status_code == 400
    assert "Rol de organizatie invalid" in response.text
    assert db.scalar(select(Invitation).where(Invitation.email == "target@test.local")) is None


def test_claim_code_is_one_time_no_store_response_not_url(client, db):
    reset_key("login_attempts:testclient")
    manager = make_user(db, email="claim-security@test.local")
    organization = make_org(db, "Claim Security Org")
    make_membership(db, manager, organization, role="organization_admin")
    station = make_station(db, organization, manager)
    db.commit()
    assert login(client, manager.email, "TestPass1234").status_code == 303

    response = client.post(
        f"/stations/{station.id}/claim-codes",
        data={"csrf_token": client.cookies["ems_csrf"]},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "new_code" not in str(response.request.url)
    assert re.search(r"EMS-[0-9A-F]{4}-[0-9A-F]{4}", response.text)


def test_sensitive_token_pages_disable_cache_and_referrers(client):
    for path in ("/accept-invitation?token=secret", "/reset-password?token=secret"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_sse_authorization_is_rechecked_after_session_or_membership_revocation(db, monkeypatch):
    user = make_user(db, email="sse-security@test.local")
    organization = make_org(db, "SSE Security Org")
    membership = make_membership(db, user, organization, role="viewer")
    station = make_station(db, organization, user)
    session, _token = auth_service.create_session(db, user, None, None)
    monkeypatch.setattr(sse.dashboard_service, "get_summary", lambda _db, _station: {"ok": True})

    assert sse._authorized_summary(db, station.id, user.id, session.id) == {"ok": True}
    db.delete(membership)
    db.flush()
    assert sse._authorized_summary(db, station.id, user.id, session.id) is None

    replacement = Membership(user_id=user.id, organization_id=organization.id, role="viewer")
    db.add(replacement)
    auth_service.revoke_session(db, session)
    db.flush()
    assert sse._authorized_summary(db, station.id, user.id, session.id) is None


def test_production_requires_secure_cookie_and_non_console_email():
    common = {
        "_env_file": None,
        "environment": "production",
        "secret_key": "production-secret",
        "opcom_use_synthetic_fixture_on_failure": False,
    }
    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        Settings(**common, session_cookie_secure=False, email_backend="smtp")
    with pytest.raises(RuntimeError, match="EMAIL_BACKEND=console"):
        Settings(**common, session_cookie_secure=True, email_backend="console")


def test_console_email_logs_metadata_without_body(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.core.email.logger.info", lambda event, **values: captured.update(event=event, **values))

    from app.core.email import ConsoleEmailAdapter

    ConsoleEmailAdapter().send("user@test.local", "subject", "token=top-secret")
    assert captured == {
        "event": "email.console_send",
        "to": "user@test.local",
        "subject": "subject",
        "body_redacted": True,
    }
