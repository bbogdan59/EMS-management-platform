"""Teste HTTP pentru administrarea membrilor/invitatiilor unei organizatii
(issue #23): schimbare de rol, dezactivare/reactivare/eliminare membership,
retrimitere/anulare invitatie -- prin rutele de autoservire
`/organizations/{id}/members/...` (organization_admin+), cu acoperire
cross-tenant, per rol, CSRF si invalidare de sesiune/SSE la dezactivare."""
from __future__ import annotations

from sqlalchemy import select

from app.core.rate_limit import reset_key
from app.models.organization import Membership
from app.services import membership_service
from tests.factories import make_membership, make_org, make_user
from tests.web_helpers import get_csrf, login


def _setup(db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Members Routes Org")
    admin = make_user(db, email="mr-admin@test.local", password="Password1234")
    other_admin = make_user(db, email="mr-admin2@test.local", password="Password1234")
    viewer = make_user(db, email="mr-viewer@test.local", password="Password1234")
    operator = make_user(db, email="mr-operator@test.local", password="Password1234")
    make_membership(db, admin, org, role="organization_admin")
    make_membership(db, other_admin, org, role="organization_admin")
    m_viewer = make_membership(db, viewer, org, role="viewer")
    make_membership(db, operator, org, role="operator")
    db.commit()
    return org, admin, other_admin, viewer, operator, m_viewer


def test_organization_admin_can_change_member_role(client, db):
    org, admin, _other_admin, _viewer, operator, _m_viewer = _setup(db)
    m_operator = db.scalar(select(Membership).where(Membership.user_id == operator.id))
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/organizations/{org.id}/members/{m_operator.id}/role",
        data={"csrf_token": csrf, "role": "organization_admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Membership, m_operator.id).role == "organization_admin"


def test_operator_cannot_manage_members(client, db):
    org, _admin, _other_admin, _viewer, operator, m_viewer = _setup(db)
    login(client, operator.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/organizations/{org.id}/members/{m_viewer.id}/deactivate",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db.expire_all()
    assert db.get(Membership, m_viewer.id).is_active is True


def test_viewer_cannot_manage_members(client, db):
    org, _admin, _other_admin, viewer, _operator, m_viewer = _setup(db)
    login(client, viewer.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/organizations/{org.id}/members/{m_viewer.id}/role",
        data={"csrf_token": csrf, "role": "operator"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_last_admin_cannot_be_demoted_via_http(client, db):
    reset_key("login_attempts:testclient")
    org = make_org(db, "Sole Admin Route Org")
    admin = make_user(db, email="sar-admin@test.local", password="Password1234")
    m_admin = make_membership(db, admin, org, role="organization_admin")
    db.commit()

    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/organizations/{org.id}/members/{m_admin.id}/role",
        data={"csrf_token": csrf, "role": "viewer"},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # redirect cu eroare, nu 200 -- dar nicio schimbare aplicata
    db.expire_all()
    assert db.get(Membership, m_admin.id).role == "organization_admin"


def test_cannot_modify_membership_from_another_organization(client, db):
    org1, admin1, _other_admin, _viewer, _operator, _m_viewer = _setup(db)
    reset_key("login_attempts:testclient")
    org2 = make_org(db, "Cross Tenant Members Org")
    victim = make_user(db, email="mr-victim@test.local", password="Password1234")
    m_victim = make_membership(db, victim, org2, role="organization_admin")
    db.commit()

    login(client, admin1.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/organizations/{org1.id}/members/{m_victim.id}/deactivate",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # nu 404/500 care ar confirma existenta -- redirect generic cu eroare
    db.expire_all()
    assert db.get(Membership, m_victim.id).is_active is True  # neatinsa


def test_deactivated_member_loses_access_immediately(client, db):
    org, admin, _other_admin, viewer, _operator, m_viewer = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(
        f"/organizations/{org.id}/members/{m_viewer.id}/deactivate",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    reset_key("login_attempts:testclient")
    login(client, viewer.email, "Password1234")
    resp = client.get(f"/organizations/{org.id}", follow_redirects=False)
    assert resp.status_code == 403  # sesiunea revocata la dezactivare -> autentificare noua necesara, apoi acces blocat


def test_deactivate_then_reactivate_restores_access(client, db):
    org, admin, _other_admin, viewer, _operator, m_viewer = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    client.post(f"/organizations/{org.id}/members/{m_viewer.id}/deactivate", data={"csrf_token": csrf}, follow_redirects=False)
    resp = client.post(f"/organizations/{org.id}/members/{m_viewer.id}/reactivate", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Membership, m_viewer.id).is_active is True

    reset_key("login_attempts:testclient")
    login(client, viewer.email, "Password1234")
    resp = client.get(f"/organizations/{org.id}")
    assert resp.status_code == 200


def test_remove_member_route(client, db):
    org, admin, _other_admin, viewer, _operator, m_viewer = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)
    resp = client.post(f"/organizations/{org.id}/members/{m_viewer.id}/remove", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Membership, m_viewer.id) is None


def test_two_active_sessions_of_the_same_member_are_both_invalidated_on_deactivation(client, db):
    """Simuleaza doua taburi/dispozitive ale aceluiasi membru -- dezactivarea
    membership-ului trebuie sa invalideze acces din AMBELE, nu doar din cel
    care a facut ultima cerere."""
    org, admin, _other_admin, viewer, _operator, m_viewer = _setup(db)
    from fastapi.testclient import TestClient

    from app.main import app

    tab1 = TestClient(app)
    tab2 = TestClient(app)
    with tab1, tab2:
        reset_key("login_attempts:testclient")
        login(tab1, viewer.email, "Password1234")
        reset_key("login_attempts:testclient")
        login(tab2, viewer.email, "Password1234")

        assert tab1.get(f"/organizations/{org.id}").status_code == 200
        assert tab2.get(f"/organizations/{org.id}").status_code == 200

        reset_key("login_attempts:testclient")
        login(client, admin.email, "Password1234")
        csrf = get_csrf(client)
        client.post(f"/organizations/{org.id}/members/{m_viewer.id}/deactivate", data={"csrf_token": csrf}, follow_redirects=False)

        # Sesiunea revocata -> neautentificat -> redirect 303 catre /login
        # (comportamentul standard al aplicatiei pentru 401 pe rute HTML,
        # vezi app/main.py::http_exception_handler), in AMBELE taburi.
        resp1 = tab1.get(f"/organizations/{org.id}", follow_redirects=False)
        resp2 = tab2.get(f"/organizations/{org.id}", follow_redirects=False)
        assert resp1.status_code == 303 and resp1.headers["location"].startswith("/login")
        assert resp2.status_code == 303 and resp2.headers["location"].startswith("/login")


def test_resend_and_cancel_invitation_routes(client, db):
    org, admin, _other_admin, _viewer, _operator, _m_viewer = _setup(db)
    login(client, admin.email, "Password1234")
    csrf = get_csrf(client)

    resp = client.post(
        f"/organizations/{org.id}/invitations",
        data={"csrf_token": csrf, "email": "pending-invite@test.local", "role": "viewer"},
    )
    assert resp.status_code == 200
    invitation = membership_service.list_pending_invitations(db, org)[0]
    old_hash = invitation.token_hash

    resp = client.post(
        f"/organizations/{org.id}/invitations/{invitation.id}/resend",
        data={"csrf_token": csrf},
    )
    assert resp.status_code == 200
    db.expire_all()
    assert membership_service.list_pending_invitations(db, org)[0].token_hash != old_hash

    resp = client.post(
        f"/organizations/{org.id}/invitations/{invitation.id}/cancel",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert membership_service.list_pending_invitations(db, org) == []


def test_member_management_routes_require_csrf(client, db):
    org, admin, _other_admin, _viewer, _operator, m_viewer = _setup(db)
    login(client, admin.email, "Password1234")

    resp = client.post(f"/organizations/{org.id}/members/{m_viewer.id}/deactivate", data={}, follow_redirects=False)
    assert resp.status_code == 403
