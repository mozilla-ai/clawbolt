import contextlib
import threading
from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_lock = threading.RLock()


def get_engine() -> Engine:
    """Return the singleton engine, creating it on first call.

    Pool tuning is defensive against silent connection drops in cloud
    environments. Hosted Postgres setups (Railway, RDS proxies, NAT
    gateways) routinely close idle TCP sockets without sending a FIN,
    leaving the client end half-open. A sync DB call from an async route
    that hits such a socket can block the event loop for the kernel's
    default ~2h TCP retransmit window: zero CPU, no logs, no crash, and
    the platform's liveness probe still passes because /health/live
    never touches the DB. ``pool_recycle`` rotates connections before
    any reasonable NAT timeout, and the TCP keepalive options let the
    kernel detect a dead socket within ~80s instead.
    """
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = create_engine(
                    settings.database_url,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    connect_args={
                        "keepalives": 1,
                        "keepalives_idle": 30,
                        "keepalives_interval": 10,
                        "keepalives_count": 5,
                    },
                )
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
