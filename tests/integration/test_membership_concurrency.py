from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditLog
from app.models.organization import Membership, Organization
from app.models.user import User
from app.services import membership_service
from tests.factories import make_membership, make_org, make_user


def test_concurrent_demotions_cannot_remove_every_organization_admin(engine):
    """Two concurrent demotions must not both pass the last-admin check."""
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    setup = Session()
    try:
        org = make_org(setup, f"Concurrent Admins {suffix}")
        actor = make_user(setup, email=f"actor-{suffix}@test.local", is_platform_admin=True)
        user1 = make_user(setup, email=f"admin1-{suffix}@test.local")
        user2 = make_user(setup, email=f"admin2-{suffix}@test.local")
        member1 = make_membership(setup, user1, org, role="organization_admin")
        member2 = make_membership(setup, user2, org, role="organization_admin")
        setup.commit()
        org_id, actor_id = org.id, actor.id
        member_ids = (member1.id, member2.id)
        user_ids = (actor.id, user1.id, user2.id)
    finally:
        setup.close()

    first_has_lock = Event()
    release_first = Event()

    def demote(membership_id, hold_lock=False):
        db = Session()
        try:
            organization = db.get(Organization, org_id)
            membership = db.get(Membership, membership_id)
            actor_user = db.get(User, actor_id)
            membership_service.change_role(db, organization, membership, "viewer", actor_user)
            if hold_lock:
                first_has_lock.set()
                assert release_first.wait(timeout=10)
            db.commit()
            return "changed"
        except membership_service.MembershipError:
            db.rollback()
            return "blocked"
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(demote, member_ids[0], True)
            assert first_has_lock.wait(timeout=10)
            second = pool.submit(demote, member_ids[1], False)
            release_first.set()
            assert sorted([first.result(timeout=10), second.result(timeout=10)]) == ["blocked", "changed"]

        verify = Session()
        try:
            active_admins = verify.scalars(
                select(Membership).where(
                    Membership.organization_id == org_id,
                    Membership.role == "organization_admin",
                    Membership.is_active.is_(True),
                )
            ).all()
            assert len(active_admins) == 1
        finally:
            verify.close()
    finally:
        cleanup = Session()
        try:
            cleanup.execute(delete(AuditLog).where(AuditLog.organization_id == org_id))
            cleanup.execute(delete(Membership).where(Membership.organization_id == org_id))
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            cleanup.commit()
        finally:
            cleanup.close()
