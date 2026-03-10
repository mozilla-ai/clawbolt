"""Tests for the FileStore base classes (PerUserStore, JsonListStore).

Verifies that the base class CRUD operations work correctly and that
concrete stores (ClientStore, MediaStore) inherit the shared behavior.
"""

import pytest

from backend.app.agent.file_store import (
    ClientData,
    ClientStore,
    JsonListStore,
    MediaData,
    MediaStore,
    PerUserStore,
    UserData,
)

# ---------------------------------------------------------------------------
# PerUserStore base
# ---------------------------------------------------------------------------


def test_per_user_store_provides_user_id_and_lock() -> None:
    """PerUserStore.__init__ sets user_id and creates an asyncio lock."""
    store = PerUserStore(user_id=42)
    assert store.user_id == 42
    assert store._lock is not None


# ---------------------------------------------------------------------------
# JsonListStore CRUD (exercised through ClientStore)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_json_list_store_list_all_empty(test_user: UserData) -> None:
    """list_all returns empty list when no records exist."""
    store = ClientStore(test_user.id)
    result = await store.list_all()
    assert result == []


@pytest.mark.asyncio()
async def test_json_list_store_create_and_list(test_user: UserData) -> None:
    """Records created via create() appear in list_all()."""
    store = ClientStore(test_user.id)
    client = await store.create(name="Alice Builder", phone="555-0101")
    result = await store.list_all()
    assert len(result) == 1
    assert result[0].name == "Alice Builder"
    assert result[0].id == client.id


@pytest.mark.asyncio()
async def test_json_list_store_get(test_user: UserData) -> None:
    """get() retrieves a single record by ID."""
    store = ClientStore(test_user.id)
    client = await store.create(name="Bob Plumber")
    fetched = await store.get(client.id)
    assert fetched is not None
    assert fetched.name == "Bob Plumber"


@pytest.mark.asyncio()
async def test_json_list_store_get_missing(test_user: UserData) -> None:
    """get() returns None for a non-existent ID."""
    store = ClientStore(test_user.id)
    assert await store.get("nonexistent") is None


@pytest.mark.asyncio()
async def test_json_list_store_update(test_user: UserData) -> None:
    """update() modifies fields and returns updated record."""
    store = ClientStore(test_user.id)
    client = await store.create(name="Carol Electrician", phone="555-0202")
    updated = await store.update(client.id, phone="555-9999")
    assert updated is not None
    assert updated.phone == "555-9999"
    assert updated.name == "Carol Electrician"
    # Verify persisted
    fetched = await store.get(client.id)
    assert fetched is not None
    assert fetched.phone == "555-9999"


@pytest.mark.asyncio()
async def test_json_list_store_update_missing(test_user: UserData) -> None:
    """update() returns None for a non-existent ID."""
    store = ClientStore(test_user.id)
    assert await store.update("ghost", name="X") is None


@pytest.mark.asyncio()
async def test_json_list_store_delete(test_user: UserData) -> None:
    """delete() removes the record and returns True."""
    store = ClientStore(test_user.id)
    client = await store.create(name="Dave Roofer")
    assert await store.delete(client.id) is True
    assert await store.get(client.id) is None
    assert await store.list_all() == []


@pytest.mark.asyncio()
async def test_json_list_store_delete_missing(test_user: UserData) -> None:
    """delete() returns False for a non-existent ID."""
    store = ClientStore(test_user.id)
    assert await store.delete("nonexistent") is False


# ---------------------------------------------------------------------------
# Inheritance verification
# ---------------------------------------------------------------------------


def test_client_store_inherits_json_list_store() -> None:
    """ClientStore is a JsonListStore subclass."""
    assert issubclass(ClientStore, JsonListStore)
    assert ClientStore._model_class is ClientData


def test_media_store_inherits_json_list_store() -> None:
    """MediaStore is a JsonListStore subclass."""
    assert issubclass(MediaStore, JsonListStore)
    assert MediaStore._model_class is MediaData


@pytest.mark.asyncio()
async def test_media_store_inherited_update(test_user: UserData) -> None:
    """MediaStore.update() works via inherited JsonListStore.update()."""
    store = MediaStore(test_user.id)
    media = await store.create(
        original_url="https://example.com/photo.jpg",
        mime_type="image/jpeg",
    )
    updated = await store.update(media.id, processed_text="A photo of a roof")
    assert updated is not None
    assert updated.processed_text == "A photo of a roof"
    assert updated.original_url == "https://example.com/photo.jpg"


@pytest.mark.asyncio()
async def test_media_store_inherited_delete(test_user: UserData) -> None:
    """MediaStore.delete() works via inherited JsonListStore.delete()."""
    store = MediaStore(test_user.id)
    media = await store.create(original_url="https://example.com/x.jpg")
    assert await store.delete(media.id) is True
    assert await store.list_all() == []
