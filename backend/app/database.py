import contextlib
import threading
from collections.abc import AsyncGenerator, Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_lock = threading.RLock()


# Pool tuning constants shared by sync and async engines.
#
# Pool tuning is defensive against silent connection drops in cloud
# environments. Hosted Postgres setups (Railway, RDS proxies, NAT
# gateways) routinely close idle TCP sockets without sending a FIN,
# leaving the client end half-open. A sync DB call from an async route
# that hits such a socket can block the event loop for the kernel's
# default ~2h TCP retransmit window: zero CPU, no logs, no crash, and
# the platform's liveness probe still passes because /health/live
# never touches the DB. ``pool_recycle`` rotates connections before
# any reasonable NAT timeout.
_POOL_RECYCLE_SECONDS = 1800
# 30s is well above the tail of legitimate queries observed in prod
# (<1s) but bounded enough that an orphaned advisory-lock wait or
# runaway query can't silently freeze a worker for hours.
_STATEMENT_TIMEOUT_MS = 30000


def _sync_database_url(url: str) -> str:
    """Translate a postgres URL to its psycopg3 sync equivalent.

    SQLAlchemy 2.x still resolves the bare ``postgresql://`` scheme to
    psycopg2 even when only psycopg3 is installed. We pin the sync
    driver to psycopg3 explicitly so deployments can keep their
    existing ``postgresql://...`` ``DATABASE_URL`` values without
    needing a rewrite, and so any explicit ``postgresql+psycopg2://``
    URLs still land on the new driver.

    Async-prefixed URLs are left alone: the async engine path handles
    them via ``_async_database_url``.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + url[len("postgresql+psycopg2://") :]
    return url


def _async_database_url(url: str) -> str:
    """Translate a sync postgres URL to its asyncpg equivalent.

    SQLAlchemy uses driver-specific URL prefixes. Sync paths use
    ``postgresql://`` or ``postgresql+psycopg://`` (psycopg3); the
    async path uses ``postgresql+asyncpg://``. Settings ship one
    ``database_url`` value; we derive the async form here so callers
    don't need to know which driver they are picking up.

    The legacy ``postgresql+psycopg2://`` prefix is also translated for
    forward compatibility with environments still pinned to psycopg2 in
    their ``DATABASE_URL``.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    return url


def get_engine() -> Engine:
    """Return the singleton sync engine, creating it on first call.

    The TCP keepalive options let the kernel detect a dead socket
    within ~80s instead of the default ~2h retransmit window. asyncpg
    sets its own keepalives by default so the async engine does not
    need the same ``connect_args`` payload.

    The URL is run through ``_sync_database_url`` so a bare
    ``postgresql://`` (which SQLAlchemy 2.x still resolves to psycopg2
    by default) is pinned to the psycopg3 driver we ship.
    """
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = create_engine(
                    _sync_database_url(settings.database_url),
                    pool_pre_ping=True,
                    pool_recycle=_POOL_RECYCLE_SECONDS,
                    connect_args={
                        "keepalives": 1,
                        "keepalives_idle": 30,
                        "keepalives_interval": 10,
                        "keepalives_count": 5,
                        # Backstop against any single statement wedging
                        # the connection (and any sync DB call inside
                        # an async route, the event loop).
                        "options": f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
                    },
                )
    return _engine


def get_async_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call.

    Mirrors ``get_engine()`` but uses asyncpg as the driver. The async
    engine coexists with the sync engine: both pull from the same
    ``settings.database_url`` value and target the same database, but
    each maintains its own connection pool. This is the foundation for
    the dual-API (sync + async) store rollout in #1150-1157.
    """
    global _async_engine
    if _async_engine is None:
        with _lock:
            if _async_engine is None:
                _async_engine = create_async_engine(
                    _async_database_url(settings.database_url),
                    pool_pre_ping=True,
                    pool_recycle=_POOL_RECYCLE_SECONDS,
                    # asyncpg uses a different connect_args shape than
                    # the sync psycopg driver. ``server_settings`` maps
                    # to libpq's ``options`` parameter; keepalives are
                    # on by default in asyncpg so we don't repeat them
                    # here.
                    connect_args={
                        "server_settings": {
                            "statement_timeout": str(_STATEMENT_TIMEOUT_MS),
                        },
                    },
                )
    return _async_engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the singleton session factory, creating it on first call."""
    global _SessionLocal
    if _SessionLocal is None:
        with _lock:
            if _SessionLocal is None:
                _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the singleton async session factory, creating it on first call."""
    global _async_session_factory
    if _async_session_factory is None:
        with _lock:
            if _async_session_factory is None:
                _async_session_factory = async_sessionmaker(
                    bind=get_async_engine(),
                    autoflush=False,
                    expire_on_commit=False,
                )
    return _async_session_factory


# Convenience alias for direct use outside of FastAPI dependency injection
def SessionLocal() -> Session:
    """Create a new session from the singleton factory."""
    return get_session_factory()()


def AsyncSessionLocal() -> AsyncSession:
    """Create a new async session from the singleton factory."""
    return get_async_session_factory()()


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


@contextlib.asynccontextmanager
async def db_session_async() -> AsyncGenerator[AsyncSession]:
    """Async context manager mirroring ``db_session()``.

    Usage::

        async with db_session_async() as db:
            db.add(obj)
            await db.commit()

    Same lifecycle semantics as the sync version: rollback on
    exception, always close. The session factory's
    ``expire_on_commit=False`` matches the SQLAlchemy async-default
    recommendation (avoids implicit IO on attribute access after
    commit, which would surface as ``MissingGreenlet`` errors).
    """
    db = AsyncSessionLocal()
    try:
        yield db
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


def get_db() -> Generator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency that yields an ``AsyncSession``.

    Symmetric with ``get_db()``. Routes converted to async DB access
    can ``Depends(get_async_db)`` while routes still on sync continue
    to ``Depends(get_db)``.
    """
    db = get_async_session_factory()()
    try:
        yield db
    finally:
        await db.close()
