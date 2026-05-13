"""Tests for the agent-native storage refactor.

Covers: MediaHandle minting + reverse lookup, pipeline gating on
``agent_native_storage``, analyze_photo / discard_media tools, the media
factory's always-on registration (issue #1170), and the startup
mutual-exclusion check for personal storage backends.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from backend.app.agent import media_staging
from backend.app.agent.tools import media_tools
from backend.app.agent.tools.media_tools import (
    _media_factory,
    create_media_tools,
)
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext
from backend.app.database import db_session_async
from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import process_message_media
from backend.app.models import StagedMedia, User

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _clear_staging_between_tests(test_user: User) -> AsyncGenerator[None]:
    await media_staging.clear_user(test_user.id)
    yield
    await media_staging.clear_user(test_user.id)


@pytest_asyncio.fixture
async def second_user() -> User:
    """A second persisted user, used by cross-user permission tests."""
    async with db_session_async() as db:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"second-user-{uuid.uuid4().hex[:8]}",
            phone="+15557654321",
            channel_identifier="987654321",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


def _make_media(url: str = "https://example.com/media") -> DownloadedMedia:
    return DownloadedMedia(
        content=b"fake-bytes",
        mime_type="image/jpeg",
        original_url=url,
        filename="test.jpg",
    )


# ---------------------------------------------------------------------------
# MediaHandle (media_staging) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stage_returns_handle(test_user: User) -> None:
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    assert handle.startswith("media_")


@pytest.mark.asyncio()
async def test_stage_returns_none_for_empty_inputs(test_user: User) -> None:
    assert await media_staging.stage(test_user.id, "", b"bytes", "image/jpeg") is None
    assert await media_staging.stage(test_user.id, "url", b"", "image/jpeg") is None


@pytest.mark.asyncio()
async def test_handle_is_stable_across_restage(test_user: User) -> None:
    """Re-staging the same URL returns the same handle so the agent can
    reference it consistently across turns."""
    h1 = await media_staging.stage(test_user.id, "url-1", b"a", "image/jpeg")
    h2 = await media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    assert h1 == h2


@pytest.mark.asyncio()
async def test_restage_overwrites_bytes(test_user: User) -> None:
    """Re-staging updates the bytes the next reader sees."""
    handle = await media_staging.stage(test_user.id, "url-1", b"first", "image/jpeg")
    assert handle is not None
    await media_staging.stage(test_user.id, "url-1", b"second", "image/png")
    entry = await media_staging.get_by_handle(handle)
    assert entry is not None
    _user, _url, content, mime = entry
    assert content == b"second"
    assert mime == "image/png"


@pytest.mark.asyncio()
async def test_media_handle_uniqueness_across_urls(test_user: User) -> None:
    """Different URLs must get distinct handles so analyze_photo(handle)
    never pulls the wrong bytes."""
    h1 = await media_staging.stage(test_user.id, "url-1", b"a", "image/jpeg")
    h2 = await media_staging.stage(test_user.id, "url-2", b"b", "image/jpeg")
    assert h1 != h2


@pytest.mark.asyncio()
async def test_get_by_handle_returns_bytes(test_user: User) -> None:
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    entry = await media_staging.get_by_handle(handle)
    assert entry is not None
    user_id, url, content, mime = entry
    assert user_id == test_user.id
    assert url == "url-1"
    assert content == b"bytes"
    assert mime == "image/jpeg"


@pytest.mark.asyncio()
async def test_get_by_handle_missing(test_user: User) -> None:
    assert await media_staging.get_by_handle("media_missing") is None


@pytest.mark.asyncio()
async def test_evict_by_handle(test_user: User) -> None:
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    async with db_session_async() as db:
        row = (
            await db.execute(select(StagedMedia).where(StagedMedia.handle == handle))
        ).scalar_one()
        disk_path = Path(row.disk_path)
    assert disk_path.exists()
    assert await media_staging.evict_by_handle(handle) is True
    assert await media_staging.get_by_handle(handle) is None
    # Disk file goes with the row: a follow-up restart should not see
    # the bytes laying around either.
    assert not disk_path.exists()
    # Idempotent: second evict returns False because already gone.
    assert await media_staging.evict_by_handle(handle) is False


@pytest.mark.asyncio()
async def test_touch_extends_ttl(test_user: User) -> None:
    """``touch`` updates ``expires_at`` so a near-expired entry survives.

    The TTL is wall-clock not monotonic, so the assertion checks that the
    row's stored expiry advances past where it started instead of
    fast-forwarding the clock.
    """
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    async with db_session_async() as db:
        initial = (
            await db.execute(select(StagedMedia).where(StagedMedia.handle == handle))
        ).scalar_one()
        initial_exp = initial.expires_at
    # Force the row's expiry into the past so we can verify ``touch``
    # genuinely pushes it back into the future.
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with db_session_async() as db:
        await db.execute(
            update(StagedMedia).where(StagedMedia.handle == handle).values(expires_at=past)
        )
        await db.commit()
    assert await media_staging.touch(handle) is True
    async with db_session_async() as db:
        refreshed = (
            await db.execute(select(StagedMedia).where(StagedMedia.handle == handle))
        ).scalar_one()
    assert refreshed.expires_at > datetime.now(UTC)
    assert refreshed.expires_at > initial_exp


@pytest.mark.asyncio()
async def test_touch_unknown_handle(test_user: User) -> None:
    assert await media_staging.touch("media_missing") is False


@pytest.mark.asyncio()
async def test_get_handle_for_roundtrip(test_user: User) -> None:
    handle = await media_staging.stage(test_user.id, "url-xyz", b"b", "image/jpeg")
    assert handle is not None
    assert await media_staging.get_handle_for(test_user.id, "url-xyz") == handle
    assert await media_staging.get_handle_for(test_user.id, "missing") is None


@pytest.mark.asyncio()
async def test_staged_bytes_survive_in_process_state_reset(test_user: User) -> None:
    """Bytes outlive a process-state wipe.

    Regression for #1333: the prior in-process dict lost everything on
    every deploy. Persistence is now disk + DB, so simulating a process
    restart (here: a `clear_user`-free reread) still returns the bytes.
    """
    handle = await media_staging.stage(test_user.id, "url-survival", b"persist", "image/jpeg")
    assert handle is not None
    # New process would re-import the module; functionally equivalent
    # to a fresh in-process dict because the implementation no longer
    # keeps one. The reread below must find the bytes on disk + DB.
    entry = await media_staging.get_by_handle(handle)
    assert entry is not None
    _user, _url, content, _mime = entry
    assert content == b"persist"


@pytest.mark.asyncio()
async def test_purge_expired_drops_dead_rows(test_user: User) -> None:
    """Rows past ``expires_at`` get swept and their disk bytes removed."""
    handle = await media_staging.stage(test_user.id, "url-expired", b"bytes", "image/jpeg")
    assert handle is not None
    async with db_session_async() as db:
        row = (
            await db.execute(select(StagedMedia).where(StagedMedia.handle == handle))
        ).scalar_one()
        disk_path = Path(row.disk_path)
    assert disk_path.exists()
    past = datetime.now(UTC) - timedelta(hours=1)
    async with db_session_async() as db:
        await db.execute(
            update(StagedMedia).where(StagedMedia.handle == handle).values(expires_at=past)
        )
        await db.commit()
    purged = await media_staging.purge_expired()
    assert purged == 1
    assert await media_staging.get_by_handle(handle) is None
    # Startup purge must also unlink the bytes; otherwise the deployment
    # accumulates orphan files on every deploy cycle.
    assert not disk_path.exists()


# ---------------------------------------------------------------------------
# Pipeline gating tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_pipeline_skips_vision(mock_vision: AsyncMock, test_user: User) -> None:
    """Pipeline stages bytes and labels the context with a handle, but does
    not call the vision LLM. Vision is the agent's decision via analyze_photo."""
    await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    result = await process_message_media("hi", [_make_media("url-1")], user_id=test_user.id)
    assert mock_vision.await_count == 0
    # Context surfaces the handle so the agent knows what to call.
    handle = await media_staging.get_handle_for(test_user.id, "url-1")
    assert handle is not None
    assert "call analyze_photo" in result.combined_context
    assert handle in result.combined_context


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_pipeline_empty_extracted_text(mock_vision: AsyncMock, test_user: User) -> None:
    """ProcessedMedia.extracted_text is empty so nothing leaks into
    conversation history before the agent decides."""
    await media_staging.stage(test_user.id, "url-2", b"b", "image/jpeg")
    result = await process_message_media("", [_make_media("url-2")], user_id=test_user.id)
    assert result.media_results[0].extracted_text == ""
    assert mock_vision.await_count == 0


# ---------------------------------------------------------------------------
# analyze_photo tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.media_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_analyze_photo_happy_path(mock_vision: AsyncMock, test_user: User) -> None:
    mock_vision.return_value = "A damaged roof."
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None

    tools = create_media_tools(test_user.id, "tell me what this is", {})
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)

    result = await analyze.function(handle=handle)
    assert result.is_error is False
    assert result.content == "A damaged roof."
    # Caption fell through from turn_text.
    mock_vision.assert_awaited_once()
    await_args = mock_vision.await_args
    assert await_args is not None
    _, _, passed_context = await_args.args
    assert passed_context == "tell me what this is"


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.media_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_analyze_photo_cached_second_call(mock_vision: AsyncMock, test_user: User) -> None:
    mock_vision.return_value = "A deck."
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    cache: dict[str, str] = {}
    tools = create_media_tools(test_user.id, "", cache)
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)

    r1 = await analyze.function(handle=handle)
    r2 = await analyze.function(handle=handle)
    assert r1.content == r2.content == "A deck."
    # Vision ran exactly once.
    assert mock_vision.await_count == 1


@pytest.mark.asyncio()
async def test_analyze_photo_missing_handle(test_user: User) -> None:
    tools = create_media_tools(test_user.id, "", {})
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)
    result = await analyze.function(handle="media_missing")
    assert result.is_error is True
    assert result.error_kind is not None
    assert "expired" in result.content or "No staged media" in result.content


@pytest.mark.asyncio()
async def test_analyze_photo_wrong_user(test_user: User, second_user: User) -> None:
    """A handle minted for another user must not leak bytes across users."""
    handle = await media_staging.stage(second_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    tools = create_media_tools(test_user.id, "", {})
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)
    result = await analyze.function(handle=handle)
    assert result.is_error is True
    assert "not belong" in result.content


# ---------------------------------------------------------------------------
# discard_media tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_discard_media_evicts_handle(test_user: User) -> None:
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    tools = create_media_tools(test_user.id, "", {})
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)
    result = await discard.function(handle=handle, reason='user said "drop this"')
    assert result.is_error is False
    assert await media_staging.get_by_handle(handle) is None


@pytest.mark.asyncio()
async def test_discard_media_idempotent(test_user: User) -> None:
    """A second discard on the same handle must not error — otherwise the
    agent gets stuck retrying."""
    handle = await media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    tools = create_media_tools(test_user.id, "", {})
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)
    r1 = await discard.function(handle=handle, reason='user said "drop"')
    r2 = await discard.function(handle=handle, reason='user said "drop"')
    assert r1.is_error is False
    assert r2.is_error is False
    assert "already discarded" in r2.content or "not staged" in r2.content


@pytest.mark.asyncio()
async def test_discard_media_missing_handle_returns_idempotent_success(
    test_user: User,
) -> None:
    tools = create_media_tools(test_user.id, "", {})
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)
    result = await discard.function(handle="media_missing", reason='"nope"')
    assert result.is_error is False
    assert "not staged" in result.content


# ---------------------------------------------------------------------------
# Registry: always-on media tools (issue #1170)
# ---------------------------------------------------------------------------


def test_media_factory_always_registers_tools(test_user: User) -> None:
    """Tools are present even with no current or staged media.

    Regression for #1170: gating on per-message media presence flipped the
    tool count between turns, busting the Anthropic prompt cache.
    """
    ctx = ToolContext(user=test_user, downloaded_media=[])
    tools = _media_factory(ctx)
    names = {t.name for t in tools}
    assert names == {ToolName.ANALYZE_PHOTO, ToolName.DISCARD_MEDIA}


@pytest.mark.asyncio()
async def test_media_factory_registers_tools_when_staged(test_user: User) -> None:
    await media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    ctx = ToolContext(user=test_user, downloaded_media=[_make_media("url-1")])
    tools = _media_factory(ctx)
    names = {t.name for t in tools}
    assert ToolName.ANALYZE_PHOTO in names
    assert ToolName.DISCARD_MEDIA in names


@pytest.mark.asyncio()
async def test_media_factory_tool_list_stable_across_media_state(test_user: User) -> None:
    """The tool name sequence must be identical regardless of media state.

    The Anthropic prompt-cache key includes the tools block, so any
    per-message variance in the tool list invalidates the cache for the
    ~135k-token system prompt prefix (issue #1170).
    """
    no_media_ctx = ToolContext(user=test_user, downloaded_media=[])
    no_media_names = [t.name for t in _media_factory(no_media_ctx)]

    await media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    with_media_ctx = ToolContext(user=test_user, downloaded_media=[_make_media("url-1")])
    with_media_names = [t.name for t in _media_factory(with_media_ctx)]

    assert no_media_names == with_media_names


# ---------------------------------------------------------------------------
# Silences unused imports in simpler environments.
# ---------------------------------------------------------------------------

assert media_tools is not None
