"""Verifica direct `_authorized_summary` (folosita de bucla de polling a
fluxului SSE, vezi `app/web/routes/sse.py::station_live_stream`) fara sa
porneasca un flux async real -- fiecare ciclu de polling re-verifica
sesiunea SI membership-ul, deci o dezactivare de membership (issue #23) sau
o revocare de sesiune trebuie sa opreasca fluxul in cel mult
`POLL_INTERVAL_SECONDS`, nu doar la o noua conexiune."""
from __future__ import annotations

from datetime import timedelta

from app.core.security import utcnow
from app.models.user import Session as UserSession
from app.services import membership_service
from app.web.routes import sse
from tests.factories import make_membership, make_org, make_station, make_user


def _session_for(db, user):
    sess = UserSession(
        user_id=user.id, token_hash="sse-test-token", csrf_secret="sse-test-csrf",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(sess)
    db.flush()
    return sess


def test_sse_summary_available_for_active_member(db):
    org = make_org(db, "SSE Active Org")
    user = make_user(db, email="sse-active@test.local")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    summary = sse._authorized_summary(db, station.id, user.id, sess.id)
    assert summary is not None


def test_sse_summary_blocked_after_membership_deactivated(db):
    org = make_org(db, "SSE Deactivate Org")
    admin = make_user(db, email="sse-admin@test.local", is_platform_admin=True)
    user = make_user(db, email="sse-target@test.local")
    membership = make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Deactivate Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    assert sse._authorized_summary(db, station.id, user.id, sess.id) is not None

    membership_service.deactivate_member(db, org, membership, admin)
    db.commit()

    assert sse._authorized_summary(db, station.id, user.id, sess.id) is None


def test_sse_summary_blocked_after_session_revoked_by_membership_action(db):
    """Dezactivarea membership-ului revoca si sesiunea (vezi
    `membership_service.deactivate_member`) -- verifica ambele cai catre
    acelasi efect: sesiunea insasi devine invalida, nu doar membership-ul."""
    org = make_org(db, "SSE Session Revoke Org")
    admin = make_user(db, email="sse-admin2@test.local", is_platform_admin=True)
    user = make_user(db, email="sse-target2@test.local")
    membership = make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Session Revoke Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    membership_service.deactivate_member(db, org, membership, admin)
    db.commit()
    db.refresh(sess)

    assert sess.revoked_at is not None
    assert sse._authorized_summary(db, station.id, user.id, sess.id) is None


def test_sse_summary_blocked_for_removed_membership(db):
    org = make_org(db, "SSE Remove Org")
    admin = make_user(db, email="sse-admin3@test.local", is_platform_admin=True)
    user = make_user(db, email="sse-target3@test.local")
    membership = make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Remove Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    membership_service.remove_member(db, org, membership, admin)
    db.commit()

    assert sse._authorized_summary(db, station.id, user.id, sess.id) is None
