from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.security import utcnow
from app.models.audit import AuditLog
from app.models.organization import Organization
from app.models.user import Session as UserSession
from app.services import organization_service as svc
from tests.factories import make_membership, make_org, make_user


def _session_for(db, user, *, revoked=False):
    sess = UserSession(
        user_id=user.id, token_hash=uuid.uuid4().hex, csrf_secret=uuid.uuid4().hex,
        expires_at=utcnow() + __import__("datetime").timedelta(hours=1),
        revoked_at=utcnow() if revoked else None,
    )
    db.add(sess)
    db.flush()
    return sess


def test_suspend_requires_nonblank_reason(db):
    org = make_org(db, "Reason Org")
    admin = make_user(db, email="reason-admin@test.local", is_platform_admin=True)
    db.commit()

    with pytest.raises(svc.OrganizationStateError):
        svc.suspend_organization(db, org, admin, "")
    with pytest.raises(svc.OrganizationStateError):
        svc.suspend_organization(db, org, admin, "   ")


def test_archive_requires_nonblank_reason(db):
    org = make_org(db, "Archive Reason Org")
    admin = make_user(db, email="archive-reason-admin@test.local", is_platform_admin=True)
    db.commit()

    with pytest.raises(svc.OrganizationStateError):
        svc.archive_organization(db, org, admin, "")


@pytest.mark.parametrize(
    "start_status,transition,end_status",
    [
        ("active", "suspend", "suspended"),
        ("active", "archive", "archived"),
        ("suspended", "reactivate", "active"),
        ("suspended", "archive", "archived"),
        ("archived", "restore", "active"),
    ],
)
def test_valid_transitions_succeed(db, start_status, transition, end_status):
    org = make_org(db, f"Transition Org {start_status}-{transition}")
    org.status = start_status
    admin = make_user(db, email=f"trans-{start_status}-{transition}@test.local", is_platform_admin=True)
    db.commit()

    if transition == "suspend":
        result = svc.suspend_organization(db, org, admin, "test reason")
    elif transition == "archive":
        result = svc.archive_organization(db, org, admin, "test reason")
    elif transition == "reactivate":
        result = svc.reactivate_organization(db, org, admin)
    else:
        result = svc.restore_organization(db, org, admin)

    assert result.status == end_status


@pytest.mark.parametrize(
    "start_status,transition",
    [
        ("active", "reactivate"),
        ("active", "restore"),
        ("suspended", "suspend"),
        ("archived", "suspend"),
        ("archived", "archive"),
    ],
)
def test_invalid_transitions_rejected(db, start_status, transition):
    org = make_org(db, f"Invalid Org {start_status}-{transition}")
    org.status = start_status
    admin = make_user(db, email=f"invalid-{start_status}-{transition}@test.local", is_platform_admin=True)
    db.commit()

    with pytest.raises(svc.OrganizationStateError):
        if transition == "suspend":
            svc.suspend_organization(db, org, admin, "reason")
        elif transition == "archive":
            svc.archive_organization(db, org, admin, "reason")
        elif transition == "reactivate":
            svc.reactivate_organization(db, org, admin)
        else:
            svc.restore_organization(db, org, admin)

    db.refresh(org)
    assert org.status == start_status, "o tranzitie invalida nu trebuie sa modifice starea"


def test_suspend_records_actor_reason_and_timestamp_and_audit(db):
    org = make_org(db, "Audit Org")
    admin = make_user(db, email="audit-admin@test.local", is_platform_admin=True)
    db.commit()

    svc.suspend_organization(db, org, admin, "client neplatitor")
    db.commit()

    assert org.status == "suspended"
    assert org.suspended_reason == "client neplatitor"
    assert org.suspended_by_user_id == admin.id
    assert org.suspended_at is not None

    entries = db.scalars(
        select(AuditLog).where(AuditLog.organization_id == org.id, AuditLog.action == "organization_suspended")
    ).all()
    assert len(entries) == 1
    assert entries[0].actor_user_id == admin.id
    assert entries[0].metadata_json["from_status"] == "active"
    assert entries[0].metadata_json["to_status"] == "suspended"


def test_reactivate_keeps_suspension_history_not_nulled(db):
    """Motivul/momentul ultimei suspendari raman ca istoric dupa reactivare --
    doar `status` reflecta starea CURENTA."""
    org = make_org(db, "History Org")
    admin = make_user(db, email="history-admin@test.local", is_platform_admin=True)
    db.commit()

    svc.suspend_organization(db, org, admin, "motiv initial")
    db.commit()
    svc.reactivate_organization(db, org, admin)
    db.commit()

    assert org.status == "active"
    assert org.suspended_reason == "motiv initial"  # pastrat, nu sters
    assert org.suspended_at is not None


def test_suspend_revokes_sessions_only_for_members_of_suspended_org(db):
    org_a = make_org(db, "Org A Sessions")
    org_b = make_org(db, "Org B Sessions")
    admin = make_user(db, email="sessions-admin@test.local", is_platform_admin=True)
    user_a = make_user(db, email="member-a@test.local")
    user_b = make_user(db, email="member-b@test.local")
    make_membership(db, user_a, org_a, role="viewer")
    make_membership(db, user_b, org_b, role="viewer")
    db.commit()

    sess_a = _session_for(db, user_a)
    sess_b = _session_for(db, user_b)
    db.commit()

    svc.suspend_organization(db, org_a, admin, "test")
    db.commit()

    db.refresh(sess_a)
    db.refresh(sess_b)
    assert sess_a.revoked_at is not None, "sesiunea membrului organizatiei suspendate trebuie revocata"
    assert sess_b.revoked_at is None, "sesiunea membrului ALTEI organizatii nu trebuie afectata"


def test_archive_also_revokes_member_sessions(db):
    org = make_org(db, "Archive Sessions Org")
    admin = make_user(db, email="archive-sessions-admin@test.local", is_platform_admin=True)
    member = make_user(db, email="archive-sessions-member@test.local")
    make_membership(db, member, org, role="operator")
    db.commit()

    sess = _session_for(db, member)
    db.commit()

    svc.archive_organization(db, org, admin, "offboarding")
    db.commit()

    db.refresh(sess)
    assert sess.revoked_at is not None


def test_update_organization_profile_updates_fields_and_audits(db):
    org = make_org(db, "Profile Org")
    admin = make_user(db, email="profile-admin@test.local", is_platform_admin=True)
    db.commit()

    svc.update_organization_profile(
        db, org, admin, name="Nume Nou", billing_email="billing@client.ro", notes="client VIP"
    )
    db.commit()

    assert org.name == "Nume Nou"
    assert org.billing_email == "billing@client.ro"
    assert org.notes == "client VIP"

    entry = db.scalar(
        select(AuditLog).where(AuditLog.organization_id == org.id, AuditLog.action == "organization_profile_updated")
    )
    assert entry is not None
    assert entry.metadata_json["after"]["name"] == "Nume Nou"


def test_concurrent_suspend_attempts_only_one_succeeds(engine):
    """Doua incercari concurente de suspendare (aceeasi organizatie, sesiuni
    DB separate) trebuie sa produca exact o tranzitie reusita -- lock-ul
    advisory PostgreSQL serializeaza, iar re-verificarea starii dupa
    achizitionarea lui respinge a doua incercare, nu produce o coliziune
    bruta sau o stare inconsistenta."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.models.organization import Organization as OrgModel
    from app.models.user import User as UserModel

    suffix = uuid.uuid4().hex
    with Session(engine) as setup:
        org = make_org(setup, f"Concurrent Org {suffix}")
        admin = make_user(setup, email=f"{suffix}@concurrency.test", is_platform_admin=True)
        setup.commit()
        org_id, admin_id = org.id, admin.id

    barrier = Barrier(2)

    def attempt():
        with Session(engine) as session:
            current_org = session.get(Organization, org_id)
            current_admin = session.get(UserModel, admin_id)
            barrier.wait(timeout=5)
            try:
                svc.suspend_organization(session, current_org, current_admin, "concurrent test")
                session.commit()
                return "ok"
            except svc.OrganizationStateError:
                session.rollback()
                return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            outcomes = [f.result(timeout=10) for f in futures]

        assert sorted(outcomes) == ["ok", "rejected"]

        with Session(engine) as verify:
            final = verify.get(Organization, org_id)
            assert final.status == "suspended"
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(AuditLog).where(AuditLog.organization_id == org_id))
            cleanup.execute(delete(OrgModel).where(OrgModel.id == org_id))
            cleanup.execute(delete(UserModel).where(UserModel.id == admin_id))
            cleanup.commit()
