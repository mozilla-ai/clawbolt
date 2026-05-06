"""Standalone sync DB utilities for the OSS test suite."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Any, cast

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_TEST_DB_URL = "postgresql+psycopg://clawbolt:clawbolt@localhost:5432/clawbolt_test"
_SYNC_TEST_ENGINE = create_engine(_TEST_DB_URL, pool_pre_ping=True)
_SYNC_TEST_SESSION_FACTORY = sessionmaker(
    bind=_SYNC_TEST_ENGINE,
    autocommit=False,
    autoflush=False,
)


def get_test_sync_engine() -> Engine:
    """Return the standalone sync engine used only by the test suite."""
    return _SYNC_TEST_ENGINE


def open_test_db_session() -> Session:
    """Open a standalone sync ``Session`` against the test database."""
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
