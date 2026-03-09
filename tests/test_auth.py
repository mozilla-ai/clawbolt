import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.agent.file_store import get_user_store
from backend.app.agent.onboarding import is_onboarding_needed
from backend.app.auth.dependencies import LOCAL_USER_ID, get_current_user
from backend.app.auth.scoping import get_scoped_user


@pytest.mark.asyncio()
async def test_get_current_user_creates_local_user() -> None:
    """OSS mode should auto-create a local user when store is empty."""
    user = await get_current_user()
    assert user.user_id == LOCAL_USER_ID
    assert user.name == ""
    assert user.id is not None


@pytest.mark.asyncio()
async def test_local_user_needs_onboarding() -> None:
    """New local user should trigger onboarding (regression for #521)."""
    user = await get_current_user()
    assert not user.onboarding_complete
    assert is_onboarding_needed(user)


@pytest.mark.asyncio()
async def test_get_current_user_returns_same_user() -> None:
    """Calling twice should return the same user."""
    c1 = await get_current_user()
    c2 = await get_current_user()
    assert c1.id == c2.id


@pytest.mark.asyncio()
async def test_get_current_user_returns_existing_telegram_user() -> None:
    """When a Telegram-created user exists, the dashboard should use it."""
    store = get_user_store()
    telegram_user = await store.create(
        user_id="telegram_123456789",
        name="Telegram User",
        channel_identifier="123456789",
        preferred_channel="telegram",
    )

    # get_current_user should return the existing user, not create a new one
    dashboard_user = await get_current_user()
    assert dashboard_user.id == telegram_user.id
    assert dashboard_user.user_id == "telegram_123456789"


def test_auth_config_returns_none_mode(client: TestClient) -> None:
    """OSS mode should return method=none."""
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    data = response.json()
    assert data == {"method": "none", "required": False}


@pytest.mark.asyncio()
async def test_scoping_returns_404_for_wrong_user() -> None:
    """Scoping should return 404 when user doesn't belong to requester."""
    store = get_user_store()
    user1 = await store.create(user_id="user-1", name="User 1")
    user2 = await store.create(user_id="user-2", name="User 2")

    # User 1 should not be able to access user 2
    with pytest.raises(HTTPException) as exc_info:
        await get_scoped_user(user1, user2.id)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio()
async def test_scoping_returns_user_for_correct_user() -> None:
    """Scoping should return user when user_id matches."""
    store = get_user_store()
    user = await store.create(user_id="user-1", name="My User")

    result = await get_scoped_user(user, user.id)
    assert result.id == user.id
