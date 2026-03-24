"""Tests for backend.app.query_helpers."""

import pytest
from fastapi import HTTPException

import backend.app.database as _db_module
from backend.app.models import User
from backend.app.query_helpers import get_or_404


def test_get_or_404_returns_row() -> None:
    """Returns the matching row when it exists."""
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="found@test.com")
        db.add(user)
        db.flush()

        result = get_or_404(db, User, id=user.id)
        assert result.user_id == "found@test.com"
    finally:
        db.close()


def test_get_or_404_raises_on_missing() -> None:
    """Raises HTTPException 404 when no row matches."""
    db = _db_module.SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc_info:
            get_or_404(db, User, detail="User not found", id="nonexistent-id")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "User not found"
    finally:
        db.close()


def test_get_or_404_default_detail() -> None:
    """Uses 'Not found' as the default detail message."""
    db = _db_module.SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc_info:
            get_or_404(db, User, id="missing")
        assert exc_info.value.detail == "Not found"
    finally:
        db.close()
