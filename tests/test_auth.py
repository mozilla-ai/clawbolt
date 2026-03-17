from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.agent.onboarding import is_onboarding_needed
from backend.app.auth.dependencies import LOCAL_USER_ID, get_current_user
from backend.app.auth.scoping import get_scoped_user
from backend.app.config import settings
from backend.app.models import User


@pytest.mark.asyncio()
async def test_get_current_user_creates_local_user() -> None:
    """OSS mode should auto-create a local user when store is empty."""
    db = _db_module.SessionLocal()
    try:
        user = await get_current_user(db)
        assert user.user_id == LOCAL_USER_ID
        assert user.id is not None
    finally:
        db.close()


@pytest.mark.asyncio()
async def test_local_user_needs_onboarding() -> None:
    """New local user should trigger onboarding (regression for #521)."""
    db = _db_module.SessionLocal()
    try:
        user = await get_current_user(db)
        assert not user.onboarding_complete
        # Create BOOTSTRAP.md to simulate file-store setup (still needed during hybrid period)
        user_dir = Path(settings.data_dir) / str(user.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "BOOTSTRAP.md").write_text("onboarding prompt", encoding="utf-8")
        assert is_onboarding_needed(user)
    finally:
        db.close()


@pytest.mark.asyncio()
async def test_get_current_user_returns_same_user() -> None:
    """Calling twice should return the same user."""
    db = _db_module.SessionLocal()
    try:
        c1 = await get_current_user(db)
        c2 = await get_current_user(db)
        assert c1.id == c2.id
    finally:
        db.close()


@pytest.mark.asyncio()
async def test_get_current_user_returns_existing_telegram_user() -> None:
    """When a Telegram-created user exists, the dashboard should use it."""
    db = _db_module.SessionLocal()
    try:
        telegram_user = User(
            user_id="telegram_123456789",
            channel_identifier="123456789",
            preferred_channel="telegram",
        )
        db.add(telegram_user)
        db.commit()
        db.refresh(telegram_user)
        db.expunge(telegram_user)
    finally:
        db.close()

    # get_current_user should return the existing user, not create a new one
    db = _db_module.SessionLocal()
    try:
        dashboard_user = await get_current_user(db)
        assert dashboard_user.id == telegram_user.id
        assert dashboard_user.user_id == "telegram_123456789"
    finally:
        db.close()


def test_auth_config_returns_none_mode(client: TestClient) -> None:
    """OSS mode should return method=none."""
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    data = response.json()
    assert data == {"method": "none", "required": False}


@pytest.mark.asyncio()
async def test_scoping_returns_404_for_wrong_user() -> None:
    """Scoping should return 404 when user doesn't belong to requester."""
    db = _db_module.SessionLocal()
    try:
        user1 = User(user_id="user-1")
        db.add(user1)
        db.commit()
        db.refresh(user1)
        db.expunge(user1)
        user2 = User(user_id="user-2")
        db.add(user2)
        db.commit()
        db.refresh(user2)
        db.expunge(user2)
    finally:
        db.close()

    # User 1 should not be able to access user 2
    db = _db_module.SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc_info:
            await get_scoped_user(user1, user2.id, db)
        assert exc_info.value.status_code == 404
    finally:
        db.close()


@pytest.mark.asyncio()
async def test_scoping_returns_user_for_correct_user() -> None:
    """Scoping should return user when user_id matches."""
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="user-1")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    db = _db_module.SessionLocal()
    try:
        result = await get_scoped_user(user, user.id, db)
        assert result.id == user.id
    finally:
        db.close()
