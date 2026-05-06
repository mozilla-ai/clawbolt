import contextlib
import threading
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_lock = threading.RLock()


# Pool tuning constants for runtime async DB access.
#
# Pool tuning is defensive against silent connection drops in cloud
# environments. Hosted Postgres setups (Railway, RDS proxies, NAT
# gateways) routinely close idle TCP sockets without sending a FIN,
# leaving the client end half-open. ``pool_recycle`` rotates
# connections before any reasonable NAT timeout.
_POOL_RECYCLE_SECONDS = 1800
# 30s is well above the tail of legitimate queries observed in prod
# (<1s) but bounded enough that an orphaned advisory-lock wait or
# runaway query can't silently freeze a worker for hours.
_STATEMENT_TIMEOUT_MS = 30000


def _sync_database_url(url: str) -> str:
    """Translate a postgres URL to its psycopg3 sync equivalent.

    Used only by alembic (offline + online migrations); no path in the
    running app uses a sync engine. SQLAlchemy 2.x still resolves the
    bare ``postgresql://`` scheme to psycopg2 even when only psycopg3
    is installed, so we pin the sync driver to psycopg3 explicitly.
    Deployments can keep their existing ``postgresql://...``
    ``DATABASE_URL`` values without needing a rewrite, and any explicit
    ``postgresql+psycopg2://`` URLs still land on the new driver.
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


# Public alias so callers outside this module (alembic env, tooling) can pin
# their URLs to psycopg3 without reaching for a leading-underscore name.
sync_database_url = _sync_database_url


def _async_database_url(url: str) -> str:
    """Translate any postgres URL flavor to its asyncpg equivalent.

    Settings ship one ``database_url`` value; we derive the async form
    here so callers don't need to know which driver they are picking up.
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


def get_async_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call.

    Uses asyncpg as the driver and shares the same defensive pool
    tuning used during the migration away from sync DB access.
    """
    global _async_engine
    if _async_engine is None:
        with _lock:
            if _async_engine is None:
                _async_engine = create_async_engine(
                    _async_database_url(settings.database_url),
                    pool_pre_ping=True,
                    pool_recycle=_POOL_RECYCLE_SECONDS,
                    connect_args={
                        "server_settings": {
                            "statement_timeout": str(_STATEMENT_TIMEOUT_MS),
                        },
                    },
                )
    return _async_engine


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


def AsyncSessionLocal() -> AsyncSession:
    """Create a new async session from the singleton factory."""
    return get_async_session_factory()()


class Base(DeclarativeBase):
    pass


@contextlib.asynccontextmanager
async def db_session_async() -> AsyncGenerator[AsyncSession]:
    """Async context manager with rollback-on-error semantics.

    Usage::

        async with db_session_async() as db:
            db.add(obj)
            await db.commit()

    The session factory's ``expire_on_commit=False`` matches the
    SQLAlchemy async-default recommendation and avoids implicit IO on
    attribute access after commit.
    """
    db = AsyncSessionLocal()
    try:
        yield db
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency that yields an ``AsyncSession``.

    Routes use this dependency directly now that runtime DB access is
    async-only.
    """
    db = get_async_session_factory()()
    try:
        yield db
    finally:
        await db.close()
