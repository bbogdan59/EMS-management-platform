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
