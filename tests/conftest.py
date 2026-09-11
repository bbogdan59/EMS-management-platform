from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://ems:ems@localhost:5432/ems_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("BOOTSTRAP_ADMIN_TOKEN", "test-bootstrap-token")
os.environ.setdefault("OPCOM_USE_SYNTHETIC_FIXTURE_ON_FAILURE", "true")
# Login rate limiting is keyed per client IP (app/web/routes/auth.py), and every
# request from Starlette's TestClient reports the same fake IP ("testclient").
# Redis (unlike the DB) is never flushed between individual tests within a run,
# so the counter accumulates across the whole test session -- once the real
# default limit (10) is exhausted by ANY combination of tests calling login(),
# every later test's login() silently fails with 429 (no session cookie set),
# and the app's 401-then-redirect-to-/login is then followed by TestClient to a
# plain 200, which can look exactly like an unrelated bug (e.g. an authorization
# check appearing to pass) to a test asserting on status codes without checking
# for a session cookie. Rate limiting itself is a production concern, not
# something the test suite's login volume should ever trip -- raised generously
# here rather than flushing Redis per test (which would also wipe state a test
# may deliberately be asserting on, like other rate-limit tests).
os.environ.setdefault("LOGIN_RATE_LIMIT_ATTEMPTS", "100000")

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import get_settings
from app.database import get_db

settings = get_settings()


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(settings.database_url)
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    connection = engine.connect()
    trans = connection.begin()
    SessionLocalTest = sessionmaker(bind=connection, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocalTest()

    nested = connection.begin_nested()

    @__import__("sqlalchemy").event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans_):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    yield session

    session.close()
    trans.rollback()
    connection.close()


@pytest.fixture()
def client(db):
    from app.main import app

    def _get_db_override():
        yield db

    app.dependency_overrides[get_db] = _get_db_override
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
