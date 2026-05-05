"""Tests for the async API of ``MediaStore`` (issue #1155).

Mirrors the sync ``MediaStore`` surface for the ``*_async`` peers
added in the dual-API rollout. All tests opt into the per-test
``async_db`` fixture (see ``tests/conftest.py``) so writes are rolled
back at teardown. Follows the IdempotencyStore pilot pattern from
PR #1199.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.stores import MediaStore
from backend.app.models import MediaFile, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count(factory: async_sessionmaker, user_id: str) -> int:
    """Return the number of MediaFile rows for the given user."""
    async with factory() as db:
        return (
            await db.scalar(select(func.count(MediaFile.id)).where(MediaFile.user_id == user_id))
        ) or 0


# ---------------------------------------------------------------------------
# create_async
# ---------------------------------------------------------------------------


async def test_async_create_inserts_row_and_allocates_id(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``create_async`` inserts a row and returns a DTO with an allocated id."""
    store = MediaStore(async_test_user.id)
    before = await _row_count(async_db, async_test_user.id)

    created = await store.create_async(
        original_url="bb_abcd",
        mime_type="image/jpeg",
        storage_path="/photos/2026",
    )

    assert created.id == "media-001"
    assert created.original_url == "bb_abcd"
    assert created.mime_type == "image/jpeg"
    assert created.storage_path == "/photos/2026"
    assert await _row_count(async_db, async_test_user.id) == before + 1


async def test_async_create_increments_id_per_user(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Successive ``create_async`` calls allocate ``media-001``, ``media-002``, ..."""
    store = MediaStore(async_test_user.id)
    a = await store.create_async(original_url="bb_a")
    b = await store.create_async(original_url="bb_b")
    c = await store.create_async(original_url="bb_c")

    assert [a.id, b.id, c.id] == ["media-001", "media-002", "media-003"]


async def test_async_create_defaults_message_id_to_empty_string(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``message_id=None`` should round-trip as ``""`` (matches sync)."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(message_id=None)
    assert created.message_id == ""


# ---------------------------------------------------------------------------
# list_all_async
# ---------------------------------------------------------------------------


async def test_async_list_all_orders_by_created_at(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``list_all_async`` returns DTOs ordered by ``created_at`` ascending."""
    store = MediaStore(async_test_user.id)
    a = await store.create_async(original_url="bb_a")
    b = await store.create_async(original_url="bb_b")
    c = await store.create_async(original_url="bb_c")

    rows = await store.list_all_async()

    ids = [r.id for r in rows]
    assert ids == [a.id, b.id, c.id]


async def test_async_list_all_empty_for_no_rows(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``list_all_async`` returns ``[]`` when no rows exist."""
    store = MediaStore(async_test_user.id)
    assert await store.list_all_async() == []


# ---------------------------------------------------------------------------
# update_async
# ---------------------------------------------------------------------------


async def test_async_update_writes_allowed_fields(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``update_async`` writes through allowlisted fields."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(original_url="bb_a")

    updated = await store.update_async(
        created.id,
        processed_text="hello world",
        storage_url="file:///tmp/a.jpg",
        storage_path="/uploads/a.jpg",
    )

    assert updated is not None
    assert updated.processed_text == "hello world"
    assert updated.storage_url == "file:///tmp/a.jpg"
    assert updated.storage_path == "/uploads/a.jpg"


async def test_async_update_skips_none_values(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``update_async`` skips fields whose value is ``None`` (matches sync)."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(original_url="bb_a", processed_text="initial")

    updated = await store.update_async(
        created.id,
        processed_text=None,
        storage_url="file:///tmp/x",
    )

    assert updated is not None
    assert updated.processed_text == "initial"
    assert updated.storage_url == "file:///tmp/x"


async def test_async_update_ignores_disallowed_fields(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``update_async`` silently drops fields outside the allowlist."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(original_url="bb_a")

    updated = await store.update_async(
        created.id,
        original_url="should-not-change",
        processed_text="ok",
    )

    assert updated is not None
    assert updated.original_url == "bb_a"
    assert updated.processed_text == "ok"


async def test_async_update_returns_none_for_missing(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``update_async`` returns ``None`` when no row matches."""
    store = MediaStore(async_test_user.id)
    assert await store.update_async("media-999", processed_text="x") is None


# ---------------------------------------------------------------------------
# get_by_url_async
# ---------------------------------------------------------------------------


async def test_async_get_by_url_matches_original_url(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_by_url_async`` matches by ``original_url``."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(original_url="bb_xyz")

    fetched = await store.get_by_url_async("bb_xyz")

    assert fetched is not None
    assert fetched.id == created.id


async def test_async_get_by_url_matches_storage_url(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_by_url_async`` also matches by ``storage_url`` (LLM round-trip path)."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(original_url="bb_xyz", storage_url="file:///tmp/photo.jpg")

    fetched = await store.get_by_url_async("file:///tmp/photo.jpg")

    assert fetched is not None
    assert fetched.id == created.id


async def test_async_get_by_url_matches_storage_path(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_by_url_async`` also matches by ``storage_path``."""
    store = MediaStore(async_test_user.id)
    created = await store.create_async(original_url="bb_xyz", storage_path="/uploads/2026")

    fetched = await store.get_by_url_async("/uploads/2026")

    assert fetched is not None
    assert fetched.id == created.id


async def test_async_get_by_url_returns_none_for_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_by_url_async`` returns ``None`` for an empty url (matches sync)."""
    store = MediaStore(async_test_user.id)
    assert await store.get_by_url_async("") is None


async def test_async_get_by_url_returns_none_for_unknown(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_by_url_async`` returns ``None`` for an unknown url."""
    store = MediaStore(async_test_user.id)
    assert await store.get_by_url_async("nope") is None


# ---------------------------------------------------------------------------
# count_by_path_prefix_async
# ---------------------------------------------------------------------------


async def test_async_count_by_path_prefix_counts_matching(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``count_by_path_prefix_async`` counts rows whose ``storage_path`` starts with the prefix."""
    store = MediaStore(async_test_user.id)
    await store.create_async(storage_path="/photos/2026/jan/a.jpg")
    await store.create_async(storage_path="/photos/2026/feb/b.jpg")
    await store.create_async(storage_path="/docs/c.pdf")

    assert await store.count_by_path_prefix_async("/photos/") == 2
    assert await store.count_by_path_prefix_async("/docs/") == 1
    assert await store.count_by_path_prefix_async("/missing/") == 0


# ---------------------------------------------------------------------------
# Per-test isolation canary
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 1: insert a row; the paired test below must not see it."""
    store = MediaStore(async_test_user.id)
    await store.create_async(original_url="iso-canary")
    rows = await store.list_all_async()
    assert any(r.original_url == "iso-canary" for r in rows)


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    store = MediaStore(async_test_user.id)
    assert await store.list_all_async() == []
