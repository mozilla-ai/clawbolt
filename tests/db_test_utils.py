"""Standalone sync DB utilities for the OSS test suite."""

from __future__ import annotations

import contextlib
import functools
import os
from collections.abc import Generator
from typing import Any, cast

import psycopg
from psycopg import sql
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_PG_ADMIN_URL = "postgresql://clawbolt:clawbolt@localhost:5432/postgres"


def _resolve_test_db_name() -> str:
    explicit = os.environ.get("OSS_TEST_DB")
    if explicit:
        return explicit
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        return f"clawbolt_test_{worker}"
    return "clawbolt_test"


_TEST_DB_NAME = _resolve_test_db_name()
_SYNC_TEST_DB_URL = f"postgresql+psycopg://clawbolt:clawbolt@localhost:5432/{_TEST_DB_NAME}"
_ASYNC_TEST_DB_URL = f"postgresql+asyncpg://clawbolt:clawbolt@localhost:5432/{_TEST_DB_NAME}"
_SYNC_TEST_ENGINE = create_engine(_SYNC_TEST_DB_URL, pool_pre_ping=True)
_SYNC_TEST_SESSION_FACTORY = sessionmaker(
    bind=_SYNC_TEST_ENGINE,
    autocommit=False,
    autoflush=False,
)


def get_test_async_db_url() -> str:
    """Return the asyncpg test database URL for this pytest worker."""
    ensure_test_database_exists()
    return _ASYNC_TEST_DB_URL


@functools.lru_cache(maxsize=1)
def ensure_test_database_exists() -> None:
    """Create the worker-specific test database if it does not already exist."""
    with psycopg.connect(_PG_ADMIN_URL, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_TEST_DB_NAME,))
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL("CREATE DATABASE {} OWNER clawbolt").format(sql.Identifier(_TEST_DB_NAME))
            )


def get_test_sync_engine() -> Engine:
    """Return the standalone sync engine used only by the test suite."""
    ensure_test_database_exists()
    return _SYNC_TEST_ENGINE


def open_test_db_session() -> Session:
    """Open a standalone sync ``Session`` against the test database."""
    ensure_test_database_exists()
    return _SYNC_TEST_SESSION_FACTORY()


@contextlib.contextmanager
def test_db_session() -> Generator[Session]:
    """Standalone sync DB context manager for test-only setup and assertions."""
    db = open_test_db_session()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


_test_db_session_for_pytest = cast(Any, test_db_session)
_test_db_session_for_pytest.__test__ = False
