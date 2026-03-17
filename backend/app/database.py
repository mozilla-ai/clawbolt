import contextlib
import threading
from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_lock = threading.Lock()


def get_engine() -> Engine:
    """Return the singleton engine, creating it on first call."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = create_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the singleton session factory, creating it on first call."""
    global _SessionLocal
    if _SessionLocal is None:
        with _lock:
            if _SessionLocal is None:
                _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


# Convenience alias for direct use outside of FastAPI dependency injection
def SessionLocal() -> Session:
    """Create a new session from the singleton factory."""
    return get_session_factory()()


class Base(DeclarativeBase):
    pass


@contextlib.contextmanager
def db_session() -> Generator[Session]:
    """Context manager that provides a DB session with proper rollback on error.

    Usage::

        with db_session() as db:
            db.add(obj)
            db.commit()
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Generator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
