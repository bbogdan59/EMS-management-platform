"""Teste HTTP pentru invitatii prin link copiabil, fara SMTP (issue #149):
crearea/regenerarea afiseaza URL-ul complet o singura data (in modul
'manual_link' mereu, in modul 'email' doar in medii non-productie), raspunsul
poarta headerele no-store/no-referrer, iar linkul nu ajunge niciodata intr-un
URL de redirect -- atat pe autoservire (`/organizations/{id}`) cat si pe
backoffice (`/admin/organizations/{id}`)."""
from __future__ import annotations

import re

from app.config import get_settings
from app.core.rate_limit import reset_key
from app.services import membership_service
from tests.factories import make_membership, make_org, make_user
from tests.web_helpers import get_csrf, login

TOKEN_RE = re.compile(r"[?&]token=([A-Za-z0-9_-]+)")


def _setup(db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Manual Link Org")
    platform_admin = make_user(db, email="mlink-platform@test.local", password="Password1234", is_platform_admin=True)
    org_admin = make_user(db, email="mlink-orgadmin@test.local", password="Password1234")
    make_membership(db, org_admin, org, role="organization_admin")
    db.commit()
    return org, platform_admin, org_admin


def test_org_self_service_reveals_link_in_manual_link_mode(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    org, _platform_admin, org_admin = _setup(db)

    login(client, org_admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "manual-invitee@test.local", "role": "viewer"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert "manual-invitee@test.local" in resp.text
    assert TOKEN_RE.search(resp.text) is not None
    assert "token=" not in str(resp.request.url)  # secretul nu apare in URL-ul cererii

    invitation = membership_service.list_pending_invitations(db, org)[0]

    resend_resp = client.post(
        f"/organizations/{org.id}/invitations/{invitation.id}/resend",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resend_resp.status_code == 200
    assert resend_resp.headers["cache-control"] == "no-store"
    assert TOKEN_RE.search(resend_resp.text) is not None


def test_org_self_service_does_not_reveal_link_in_email_mode_production(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "email")
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(
        "app.services.auth_service.get_email_adapter",
        lambda: type("A", (), {"send": staticmethod(lambda **kw: None)})(),
    )
    org, _platform_admin, org_admin = _setup(db)

    login(client, org_admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "email-mode-invitee@test.local", "role": "viewer"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert TOKEN_RE.search(resp.text) is None  # in productie, modul email nu afiseaza linkul in UI


def test_admin_backoffice_can_invite_and_reveals_link_in_manual_link_mode(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    org, platform_admin, _org_admin = _setup(db)

    login(client, platform_admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/admin/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "admin-manual-invitee@test.local", "role": "operator"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert "admin-manual-invitee@test.local" in resp.text
    assert TOKEN_RE.search(resp.text) is not None

    invitation = membership_service.list_pending_invitations(db, org)[0]
    resend_resp = client.post(
        f"/admin/organizations/{org.id}/invitations/{invitation.id}/resend",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resend_resp.status_code == 200
    assert TOKEN_RE.search(resend_resp.text) is not None


def test_admin_invite_route_requires_platform_admin(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    org, _platform_admin, org_admin = _setup(db)

    login(client, org_admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/admin/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "should-not-work@test.local", "role": "viewer"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_invite_route_requires_csrf(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    org, platform_admin, _org_admin = _setup(db)

    login(client, platform_admin.email, "Password1234")
    resp = client.post(
        f"/admin/organizations/{org.id}/invitations",
        data={"email": "no-csrf@test.local", "role": "viewer"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_double_submit_reuses_pending_invitation_instead_of_duplicating(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    org, _platform_admin, org_admin = _setup(db)

    login(client, org_admin.email, "Password1234")
    csrf = get_csrf(client)

    first = client.post(
        f"/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "double-submit@test.local", "role": "viewer"},
        follow_redirects=False,
    )
    second = client.post(
        f"/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "double-submit@test.local", "role": "viewer"},
        follow_redirects=False,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_token = TOKEN_RE.search(first.text).group(1)
    second_token = TOKEN_RE.search(second.text).group(1)
    assert first_token != second_token  # rotit, nu duplicat -- linkul vechi devine invalid

    pending = membership_service.list_pending_invitations(db, org)
    assert len(pending) == 1


def test_admin_cannot_invite_with_platform_admin_role(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    org, platform_admin, _org_admin = _setup(db)

    login(client, platform_admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/admin/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "no-global-role@test.local", "role": "platform_admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert membership_service.list_pending_invitations(db, org) == []
