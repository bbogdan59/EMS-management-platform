from __future__ import annotations

import pytest

from app.services import auth_service
from tests.factories import make_user
from app.core.rbac import role_at_least, can_manage_station_config, can_modify_operational_settings


def test_authenticate_success(db):
    user = make_user(db, email="ok@test.local", password="CorrectPass123")
    result = auth_service.authenticate(db, "ok@test.local", "CorrectPass123")
    assert result.id == user.id


def test_authenticate_wrong_password(db):
    make_user(db, email="wrong@test.local", password="CorrectPass123")
    with pytest.raises(auth_service.InvalidCredentials):
        auth_service.authenticate(db, "wrong@test.local", "BadPassword")


def test_authenticate_locks_after_repeated_failures(db):
    make_user(db, email="lock@test.local", password="CorrectPass123")
    for _ in range(auth_service.MAX_FAILED_ATTEMPTS):
        with pytest.raises(auth_service.InvalidCredentials):
            auth_service.authenticate(db, "lock@test.local", "wrong")
    with pytest.raises(auth_service.AccountLocked):
        auth_service.authenticate(db, "lock@test.local", "CorrectPass123")


def test_bootstrap_admin_requires_correct_token(db):
    with pytest.raises(auth_service.AuthError):
        auth_service.bootstrap_first_admin(db, "wrong-token", "a@test.local", "SomePass1234", "A")


def test_bootstrap_admin_only_once(db):
    user = auth_service.bootstrap_first_admin(db, "test-bootstrap-token", "first@test.local", "SomePass1234", "First")
    assert user.is_platform_admin
    with pytest.raises(auth_service.AuthError):
        auth_service.bootstrap_first_admin(db, "test-bootstrap-token", "second@test.local", "SomePass1234", "Second")


def test_role_ranking():
    assert role_at_least("organization_admin", "viewer")
    assert not role_at_least("viewer", "operator")
    assert can_manage_station_config("organization_admin")
    assert not can_manage_station_config("operator")
    assert can_modify_operational_settings("operator")
    assert not can_modify_operational_settings("viewer")


def test_password_reset_flow(db):
    user = make_user(db, email="reset@test.local", password="OldPassword123")
    auth_service.request_password_reset(db, "reset@test.local")

    from sqlalchemy import select
    from app.models.user import PasswordResetToken

    token_row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    assert token_row is not None
    # Tokenul brut nu e recuperabil din DB (doar hash) -- testam ciclul cu un token generat manual echivalent.
    from app.core.security import generate_opaque_token, hash_token

    raw = generate_opaque_token()
    token_row.token_hash = hash_token(raw)
    db.add(token_row)
    db.flush()

    auth_service.reset_password(db, raw, "BrandNewPassword123")
    refreshed = auth_service.authenticate(db, "reset@test.local", "BrandNewPassword123")
    assert refreshed.id == user.id

    with pytest.raises(auth_service.AuthError):
        auth_service.reset_password(db, raw, "AnotherPassword123")
