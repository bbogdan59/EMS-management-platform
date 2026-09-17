from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.core.security import expires_in, generate_opaque_token, hash_token, utcnow
from app.models.audit import AuditLog
from app.models.organization import Membership
from app.models.user import Invitation
from app.models.user import Session as UserSession
from app.services import membership_service as svc
from tests.factories import make_membership, make_org, make_user

_INVITER_COUNTER = 0


def _invitation(db, org, email="invited@test.local", role="viewer", *, expired=False, revoked=False, accepted=False):
    global _INVITER_COUNTER
    _INVITER_COUNTER += 1
    inviter = make_user(db, email=f"inviter{_INVITER_COUNTER}@test.local")
    inv = Invitation(
        organization_id=org.id, email=email, role=role,
        token_hash=hash_token(generate_opaque_token()),
        invited_by_user_id=inviter.id,
        expires_at=utcnow() - timedelta(hours=1) if expired else expires_in(hours=48),
        revoked_at=utcnow() if revoked else None,
        accepted_at=utcnow() if accepted else None,
    )
    db.add(inv)
    db.flush()
    return inv


def _session_for(db, user):
    sess = UserSession(
        user_id=user.id, token_hash=uuid.uuid4().hex, csrf_secret=uuid.uuid4().hex,
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(sess)
    db.flush()
    return sess


def test_list_members_flags_sole_active_admin(db):
    org = make_org(db, "Sole Admin Org")
    admin_user = make_user(db, email="sole-admin@test.local")
    viewer_user = make_user(db, email="sole-viewer@test.local")
    make_membership(db, admin_user, org, role="organization_admin")
    make_membership(db, viewer_user, org, role="viewer")
    db.commit()

    rows = {r["email"]: r for r in svc.list_members(db, org)}
    assert rows["sole-admin@test.local"]["is_sole_admin"] is True
    assert rows["sole-viewer@test.local"]["is_sole_admin"] is False


def test_list_members_does_not_flag_sole_admin_when_two_admins_exist(db):
    org = make_org(db, "Two Admins Org")
    admin1 = make_user(db, email="admin1@test.local")
    admin2 = make_user(db, email="admin2@test.local")
    make_membership(db, admin1, org, role="organization_admin")
    make_membership(db, admin2, org, role="organization_admin")
    db.commit()

    rows = {r["email"]: r for r in svc.list_members(db, org)}
    assert rows["admin1@test.local"]["is_sole_admin"] is False
    assert rows["admin2@test.local"]["is_sole_admin"] is False


def test_change_role_updates_role_revokes_sessions_and_audits(db):
    org = make_org(db, "Role Change Org")
    admin = make_user(db, email="rc-admin@test.local", is_platform_admin=True)
    admin2 = make_user(db, email="rc-admin2@test.local")
    member_user = make_user(db, email="rc-member@test.local")
    make_membership(db, admin2, org, role="organization_admin")
    membership = make_membership(db, member_user, org, role="viewer")
    db.commit()
    sess = _session_for(db, member_user)
    db.commit()

    svc.change_role(db, org, membership, "operator", admin)
    db.commit()

    assert membership.role == "operator"
    db.refresh(sess)
    assert sess.revoked_at is not None

    entry = db.scalar(
        select(AuditLog).where(AuditLog.action == "membership_role_changed", AuditLog.organization_id == org.id)
    )
    assert entry is not None
    assert entry.metadata_json["from_role"] == "viewer"
    assert entry.metadata_json["to_role"] == "operator"


def test_change_role_rejects_platform_admin_as_target(db):
    org = make_org(db, "No Platform Admin Org")
    admin = make_user(db, email="npa-admin@test.local", is_platform_admin=True)
    member_user = make_user(db, email="npa-member@test.local")
    membership = make_membership(db, member_user, org, role="viewer")
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.change_role(db, org, membership, "platform_admin", admin)


def test_change_role_rejects_demoting_last_active_admin(db):
    org = make_org(db, "Last Admin Demote Org")
    admin = make_user(db, email="lad-admin@test.local", is_platform_admin=True)
    sole_admin_user = make_user(db, email="lad-sole@test.local")
    membership = make_membership(db, sole_admin_user, org, role="organization_admin")
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.change_role(db, org, membership, "viewer", admin)
    db.refresh(membership)
    assert membership.role == "organization_admin"


def test_change_role_allows_demoting_admin_when_another_admin_remains(db):
    org = make_org(db, "Safe Demote Org")
    admin = make_user(db, email="sd-admin@test.local", is_platform_admin=True)
    user1 = make_user(db, email="sd-user1@test.local")
    user2 = make_user(db, email="sd-user2@test.local")
    m1 = make_membership(db, user1, org, role="organization_admin")
    make_membership(db, user2, org, role="organization_admin")
    db.commit()

    svc.change_role(db, org, m1, "viewer", admin)
    db.commit()
    assert m1.role == "viewer"


def test_deactivate_member_protects_last_admin(db):
    org = make_org(db, "Deactivate Last Admin Org")
    admin = make_user(db, email="dla-admin@test.local", is_platform_admin=True)
    sole_admin_user = make_user(db, email="dla-sole@test.local")
    membership = make_membership(db, sole_admin_user, org, role="organization_admin")
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.deactivate_member(db, org, membership, admin)
    db.refresh(membership)
    assert membership.is_active is True


def test_deactivate_member_revokes_sessions_and_blocks_reuse(db):
    org = make_org(db, "Deactivate Org")
    admin = make_user(db, email="deact-admin@test.local", is_platform_admin=True)
    member_user = make_user(db, email="deact-member@test.local")
    membership = make_membership(db, member_user, org, role="viewer")
    db.commit()
    sess = _session_for(db, member_user)
    db.commit()

    svc.deactivate_member(db, org, membership, admin)
    db.commit()

    assert membership.is_active is False
    db.refresh(sess)
    assert sess.revoked_at is not None

    entry = db.scalar(
        select(AuditLog).where(AuditLog.action == "membership_deactivated", AuditLog.organization_id == org.id)
    )
    assert entry is not None


def test_deactivate_member_revokes_every_active_session_not_just_one(db):
    """Simuleaza "doua taburi" (doua sesiuni active ale aceluiasi membru,
    ex. doua browsere/dispozitive) -- dezactivarea membership-ului trebuie
    sa invalideze TOATE, nu doar una, altfel accesul ramane deschis in
    tab-ul neafectat."""
    org = make_org(db, "Two Tabs Org")
    admin = make_user(db, email="tt-admin@test.local", is_platform_admin=True)
    member_user = make_user(db, email="tt-member@test.local")
    membership = make_membership(db, member_user, org, role="viewer")
    db.commit()
    tab1 = _session_for(db, member_user)
    tab2 = _session_for(db, member_user)
    db.commit()

    svc.deactivate_member(db, org, membership, admin)
    db.commit()

    db.refresh(tab1)
    db.refresh(tab2)
    assert tab1.revoked_at is not None
    assert tab2.revoked_at is not None


def test_reactivate_member_restores_access(db):
    org = make_org(db, "Reactivate Org")
    admin = make_user(db, email="react-admin@test.local", is_platform_admin=True)
    member_user = make_user(db, email="react-member@test.local")
    membership = make_membership(db, member_user, org, role="viewer")
    membership.is_active = False
    db.commit()

    svc.reactivate_member(db, org, membership, admin)
    db.commit()
    assert membership.is_active is True


def test_remove_member_protects_last_admin(db):
    org = make_org(db, "Remove Last Admin Org")
    admin = make_user(db, email="rla-admin@test.local", is_platform_admin=True)
    sole_admin_user = make_user(db, email="rla-sole@test.local")
    membership = make_membership(db, sole_admin_user, org, role="organization_admin")
    membership_id = membership.id
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.remove_member(db, org, membership, admin)
    assert db.get(Membership, membership_id) is not None


def test_remove_member_hard_deletes_and_revokes_sessions(db):
    org = make_org(db, "Remove Org")
    admin = make_user(db, email="rem-admin@test.local", is_platform_admin=True)
    member_user = make_user(db, email="rem-member@test.local")
    membership = make_membership(db, member_user, org, role="viewer")
    membership_id = membership.id
    db.commit()
    sess = _session_for(db, member_user)
    db.commit()

    svc.remove_member(db, org, membership, admin)
    db.commit()

    assert db.get(Membership, membership_id) is None
    db.refresh(sess)
    assert sess.revoked_at is not None

    entry = db.scalar(
        select(AuditLog).where(AuditLog.action == "membership_removed", AuditLog.organization_id == org.id)
    )
    assert entry is not None
    assert entry.metadata_json["user_id"] == str(member_user.id)


def test_list_pending_invitations_excludes_accepted_and_revoked(db):
    org = make_org(db, "Pending Invites Org")
    pending = _invitation(db, org, email="pending@test.local")
    _invitation(db, org, email="accepted@test.local", accepted=True)
    _invitation(db, org, email="revoked@test.local", revoked=True)
    db.commit()

    pending_ids = {inv.id for inv in svc.list_pending_invitations(db, org)}
    assert pending_ids == {pending.id}


def test_resend_invitation_regenerates_token_and_expiry(db):
    org = make_org(db, "Resend Org")
    admin = make_user(db, email="resend-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="resend-target@test.local", expired=True)
    old_hash = inv.token_hash
    db.commit()

    raw_token = svc.resend_invitation(db, org, inv, admin)
    db.commit()

    assert inv.token_hash != old_hash
    assert inv.token_hash == hash_token(raw_token)
    assert inv.expires_at > utcnow()

    entry = db.scalar(
        select(AuditLog).where(AuditLog.action == "invitation_resent", AuditLog.organization_id == org.id)
    )
    assert entry is not None


def test_resend_invitation_rejects_already_accepted(db):
    org = make_org(db, "Resend Accepted Org")
    admin = make_user(db, email="resend-acc-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="already@test.local", accepted=True)
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.resend_invitation(db, org, inv, admin)


def test_resend_invitation_sends_email_in_email_mode(db, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "app.core.email.get_email_adapter",
        lambda: type("A", (), {"send": staticmethod(lambda **kw: sent.update(kw))})(),
    )
    org = make_org(db, "Resend Email Org")
    admin = make_user(db, email="resend-email-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="resend-email-target@test.local")
    db.commit()

    raw_token = svc.resend_invitation(db, org, inv, admin)
    db.commit()

    assert sent
    assert sent["to"] == "resend-email-target@test.local"
    assert raw_token in sent["body"]


def test_resend_invitation_does_not_touch_email_adapter_in_manual_link_mode(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invitation_delivery_mode", "manual_link")
    monkeypatch.setattr(
        "app.core.email.get_email_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("email adapter should not be used in manual_link mode")),
    )
    org = make_org(db, "Resend Manual Org")
    admin = make_user(db, email="resend-manual-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="resend-manual-target@test.local")
    old_hash = inv.token_hash
    db.commit()

    raw_token = svc.resend_invitation(db, org, inv, admin)
    db.commit()

    assert inv.token_hash != old_hash
    assert inv.token_hash == hash_token(raw_token)


def test_resend_invitation_rotation_invalidates_old_token(db):
    org = make_org(db, "Rotation Org")
    admin = make_user(db, email="rotation-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="rotation-target@test.local")
    old_raw = generate_opaque_token()
    inv.token_hash = hash_token(old_raw)
    db.commit()

    new_raw = svc.resend_invitation(db, org, inv, admin)
    db.commit()

    assert new_raw != old_raw
    assert inv.token_hash == hash_token(new_raw)
    assert inv.token_hash != hash_token(old_raw)


def test_cancel_invitation_sets_revoked_and_audits(db):
    org = make_org(db, "Cancel Org")
    admin = make_user(db, email="cancel-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="cancel-target@test.local")
    db.commit()

    svc.cancel_invitation(db, org, inv, admin)
    db.commit()

    assert inv.revoked_at is not None
    entry = db.scalar(
        select(AuditLog).where(AuditLog.action == "invitation_cancelled", AuditLog.organization_id == org.id)
    )
    assert entry is not None


def test_cancel_invitation_rejects_already_accepted(db):
    org = make_org(db, "Cancel Accepted Org")
    admin = make_user(db, email="cancel-acc-admin@test.local", is_platform_admin=True)
    inv = _invitation(db, org, email="cancel-acc-target@test.local", accepted=True)
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.cancel_invitation(db, org, inv, admin)


def test_membership_actions_reject_membership_from_another_organization(db):
    org1 = make_org(db, "Cross Org 1")
    org2 = make_org(db, "Cross Org 2")
    admin = make_user(db, email="cross-admin@test.local", is_platform_admin=True)
    user1 = make_user(db, email="cross-user1@test.local")
    membership = make_membership(db, user1, org1, role="viewer")
    db.commit()

    with pytest.raises(svc.MembershipError):
        svc.change_role(db, org2, membership, "operator", admin)
    with pytest.raises(svc.MembershipError):
        svc.deactivate_member(db, org2, membership, admin)
    with pytest.raises(svc.MembershipError):
        svc.remove_member(db, org2, membership, admin)
