"""Tests for backend.app.query_helpers."""

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models import User
from backend.app.query_helpers import get_or_404


@pytest.mark.asyncio()
async def test_get_or_404_returns_row(async_db: async_sessionmaker) -> None:
    """Returns the matching row when it exists."""
    async with async_db() as db:
        user = User(user_id="found@test.com")
        db.add(user)
        await db.commit()

        result = await get_or_404(db, User, id=user.id)
    assert result.user_id == "found@test.com"


@pytest.mark.asyncio()
async def test_get_or_404_raises_on_missing(async_db: async_sessionmaker) -> None:
    """Raises HTTPException 404 when no row matches."""
    async with async_db() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_or_404(db, User, detail="User not found", id="nonexistent-id")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "User not found"


@pytest.mark.asyncio()
async def test_get_or_404_default_detail(async_db: async_sessionmaker) -> None:
    """Uses 'Not found' as the default detail message."""
    async with async_db() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_or_404(db, User, id="missing")
    assert exc_info.value.detail == "Not found"
