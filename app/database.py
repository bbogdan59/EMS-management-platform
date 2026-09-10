from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_engine_kwargs = {
    "pool_pre_ping": True,
    "echo": settings.sql_echo,
}
if not settings.database_url.startswith("sqlite"):
    _engine_kwargs["pool_size"] = settings.database_pool_size
    _engine_kwargs["max_overflow"] = settings.database_max_overflow

engine = create_engine(settings.database_url, **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """Dependency FastAPI: o sesiune per cerere, inchisa automat."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager pentru scripturi/taskuri Celery in afara FastAPI."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
