import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.agent.file_store import get_contractor_store
from backend.app.auth.dependencies import LOCAL_USER_ID, get_current_user
from backend.app.auth.scoping import get_user_contractor


@pytest.mark.asyncio()
async def test_get_current_user_creates_local_contractor() -> None:
    """OSS mode should auto-create a local contractor."""
    contractor = await get_current_user()
    assert contractor.user_id == LOCAL_USER_ID
    assert contractor.name == "Local Contractor"
    assert contractor.id is not None


@pytest.mark.asyncio()
async def test_get_current_user_returns_same_contractor() -> None:
    """Calling twice should return the same contractor."""
    c1 = await get_current_user()
    c2 = await get_current_user()
    assert c1.id == c2.id


def test_auth_config_returns_none_mode(client: TestClient) -> None:
    """OSS mode should return method=none."""
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    data = response.json()
    assert data == {"method": "none", "required": False}


@pytest.mark.asyncio()
async def test_scoping_returns_404_for_wrong_user() -> None:
    """Scoping should return 404 when contractor doesn't belong to user."""
    store = get_contractor_store()
    contractor1 = await store.create(user_id="user-1", name="Contractor 1")
    contractor2 = await store.create(user_id="user-2", name="Contractor 2")

    # User 1 should not be able to access contractor 2
    with pytest.raises(HTTPException) as exc_info:
        await get_user_contractor(contractor1, contractor2.id)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio()
async def test_scoping_returns_contractor_for_correct_user() -> None:
    """Scoping should return contractor when user_id matches."""
    store = get_contractor_store()
    contractor = await store.create(user_id="user-1", name="My Contractor")

    result = await get_user_contractor(contractor, contractor.id)
    assert result.id == contractor.id
