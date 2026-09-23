"""Teste HTTP pentru crearea unui utilizator platform_admin din admin panel
(cerere client: 'as vrea sa pot adauga si utilizatori platform Admini')."""
from __future__ import annotations

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.audit import AuditLog
from app.models.user import User
from tests.factories import make_org, make_user
from tests.web_helpers import get_csrf, login


def _setup(db):
    reset_key("login_attempts:testclient")
    admin = make_user(db, email="platadmroutes-admin@test.local", password="Password1234", is_platform_admin=True)
    org = make_org(db, "Org PlatAdm Routes")
    org_admin = make_user(db, email="platadmroutes-orgadmin@test.local", password="Password1234")
    db.commit()
    return admin, org, org_admin


def test_create_platform_admin_requires_platform_admin(client, db):
    admin, org, org_admin = _setup(db)

    login(client, org_admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        "/admin/users/platform-admins",
        data={
            "csrf_token": csrf, "email": "newadmin@test.local", "full_name": "New Admin",
            "password": "Password1234", "password_confirm": "Password1234",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert db.scalar(select(User).where(User.email == "newadmin@test.local")) is None


def test_create_platform_admin_success_grants_full_access_and_audits(client, db):
    admin, org, org_admin = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        "/admin/users/platform-admins",
        data={
            "csrf_token": csrf, "email": "NewAdmin@Test.local", "full_name": "New Admin",
            "password": "Password1234", "password_confirm": "Password1234",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"

    created = db.scalar(select(User).where(User.email == "newadmin@test.local"))
    assert created is not None
    assert created.is_platform_admin is True

    entries = db.scalars(select(AuditLog).where(AuditLog.action == "platform_admin_created")).all()
    assert any(e.metadata_json.get("email") == "newadmin@test.local" for e in entries)

    # noul cont chiar are acces de admin la toata platforma, nu doar flag-ul setat
    reset_key("login_attempts:testclient")
    login(client, "newadmin@test.local", "Password1234")
    resp = client.get(f"/admin/organizations/{org.id}")
    assert resp.status_code == 200


def test_create_platform_admin_rejects_duplicate_email(client, db):
    admin, org, org_admin = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        "/admin/users/platform-admins",
        data={
            "csrf_token": csrf, "email": org_admin.email, "full_name": "Dup",
            "password": "Password1234", "password_confirm": "Password1234",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    db.expire_all()
    assert db.get(User, org_admin.id).is_platform_admin is False


def test_create_platform_admin_rejects_short_or_mismatched_password(client, db):
    admin, org, org_admin = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        "/admin/users/platform-admins",
        data={
            "csrf_token": csrf, "email": "short@test.local", "full_name": "Short",
            "password": "short", "password_confirm": "short",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert db.scalar(select(User).where(User.email == "short@test.local")) is None

    resp = client.post(
        "/admin/users/platform-admins",
        data={
            "csrf_token": csrf, "email": "mismatch@test.local", "full_name": "Mismatch",
            "password": "Password1234", "password_confirm": "Password5678",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert db.scalar(select(User).where(User.email == "mismatch@test.local")) is None


def test_create_platform_admin_route_requires_csrf(client, db):
    admin, org, org_admin = _setup(db)
    login(client, admin.email, "Password1234")

    resp = client.post(
        "/admin/users/platform-admins",
        data={
            "email": "nocsrf@test.local", "full_name": "No Csrf",
            "password": "Password1234", "password_confirm": "Password1234",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert db.scalar(select(User).where(User.email == "nocsrf@test.local")) is None
