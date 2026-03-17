"""Tests for the DB-backed store CRUD operations.

Verifies that ClientStore and MediaStore handle basic operations correctly.
"""

import pytest

from backend.app.agent.client_db import ClientStore
from backend.app.agent.dto import UserData
from backend.app.agent.stores import MediaStore

# ---------------------------------------------------------------------------
# ClientStore CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_client_store_list_all_empty(test_user: UserData) -> None:
    """list_all returns empty list when no records exist."""
    store = ClientStore(test_user.id)
    result = await store.list_all()
    assert result == []


@pytest.mark.asyncio()
async def test_client_store_create_and_list(test_user: UserData) -> None:
    """Records created via create() appear in list_all()."""
    store = ClientStore(test_user.id)
    client = await store.create(name="Alice Builder", phone="555-0101")
    result = await store.list_all()
    assert len(result) == 1
    assert result[0].name == "Alice Builder"
    assert result[0].id == client.id


@pytest.mark.asyncio()
async def test_client_store_get(test_user: UserData) -> None:
    """get() retrieves a single record by ID."""
    store = ClientStore(test_user.id)
    client = await store.create(name="Bob Plumber")
    fetched = await store.get(client.id)
    assert fetched is not None
    assert fetched.name == "Bob Plumber"


@pytest.mark.asyncio()
async def test_client_store_get_missing(test_user: UserData) -> None:
    """get() returns None for a non-existent ID."""
    store = ClientStore(test_user.id)
    assert await store.get("nonexistent") is None


# ---------------------------------------------------------------------------
# MediaStore CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_media_store_create_and_list(test_user: UserData) -> None:
    """Records created via create() appear in list_all()."""
    store = MediaStore(test_user.id)
    media = await store.create(
        original_url="https://example.com/photo.jpg",
        mime_type="image/jpeg",
    )
    result = await store.list_all()
    assert len(result) == 1
    assert result[0].original_url == "https://example.com/photo.jpg"
    assert result[0].id == media.id


@pytest.mark.asyncio()
async def test_media_store_get_by_url(test_user: UserData) -> None:
    """get_by_url() retrieves a single record by original_url."""
    store = MediaStore(test_user.id)
    await store.create(
        original_url="https://example.com/photo.jpg",
        mime_type="image/jpeg",
    )
    fetched = await store.get_by_url("https://example.com/photo.jpg")
    assert fetched is not None
    assert fetched.mime_type == "image/jpeg"


@pytest.mark.asyncio()
async def test_media_store_get_by_url_missing(test_user: UserData) -> None:
    """get_by_url() returns None for a non-existent URL."""
    store = MediaStore(test_user.id)
    assert await store.get_by_url("nonexistent") is None
