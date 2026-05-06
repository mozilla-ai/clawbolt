"""Tests for backend.app.query_helpers."""

import pytest
from fastapi import HTTPException

from backend.app.database import db_session_async
from backend.app.models import User
from backend.app.query_helpers import get_or_404_async


async def test_get_or_404_returns_row() -> None:
    """Returns the matching row when it exists."""
    async with db_session_async() as db:
        user = User(user_id="found@test.com")
        db.add(user)
        await db.flush()

        result = await get_or_404_async(db, User, id=user.id)
        assert result.user_id == "found@test.com"


async def test_get_or_404_raises_on_missing() -> None:
    """Raises HTTPException 404 when no row matches."""
    async with db_session_async() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_or_404_async(db, User, detail="User not found", id="nonexistent-id")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "User not found"


async def test_get_or_404_default_detail() -> None:
    """Uses 'Not found' as the default detail message."""
    async with db_session_async() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_or_404_async(db, User, id="missing")
        assert exc_info.value.detail == "Not found"
