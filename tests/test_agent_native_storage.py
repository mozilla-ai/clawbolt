"""Tests for the agent-native storage refactor.

Covers: MediaHandle minting + reverse lookup, pipeline gating on
``agent_native_storage``, analyze_photo / discard_media tools, the registry
predicate that gates the media factory on staged media, and the startup
mutual-exclusion check for personal storage backends.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent import media_staging
from backend.app.agent.tools import media_tools
from backend.app.agent.tools.media_tools import (
    _media_factory,
    _reason_has_quoted_phrase,
    create_media_tools,
)
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext
from backend.app.config import Settings, validate_personal_storage_backend
from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import process_message_media
from backend.app.models import User

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_staging_between_tests(test_user: User) -> Generator[None]:
    media_staging.clear_user(test_user.id)
    media_staging.clear_user("user-a")
    media_staging.clear_user("user-b")
    yield
    media_staging.clear_user(test_user.id)
    media_staging.clear_user("user-a")
    media_staging.clear_user("user-b")


@pytest.fixture()
def agent_native_on() -> Generator[None]:
    """Turn on the agent_native_storage flag for the duration of a test."""
    from backend.app.config import settings

    prior = settings.agent_native_storage
    settings.agent_native_storage = True
    try:
        yield
    finally:
        settings.agent_native_storage = prior


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


def test_stage_returns_handle(test_user: User) -> None:
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    assert handle.startswith("media_")


def test_stage_returns_none_for_empty_inputs(test_user: User) -> None:
    assert media_staging.stage(test_user.id, "", b"bytes", "image/jpeg") is None
    assert media_staging.stage(test_user.id, "url", b"", "image/jpeg") is None


def test_handle_is_stable_across_restage(test_user: User) -> None:
    """Re-staging the same URL returns the same handle so the agent can
    reference it consistently across turns."""
    h1 = media_staging.stage(test_user.id, "url-1", b"a", "image/jpeg")
    h2 = media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    assert h1 == h2


def test_media_handle_uniqueness_across_urls(test_user: User) -> None:
    """Different URLs must get distinct handles so analyze_photo(handle)
    never pulls the wrong bytes."""
    h1 = media_staging.stage(test_user.id, "url-1", b"a", "image/jpeg")
    h2 = media_staging.stage(test_user.id, "url-2", b"b", "image/jpeg")
    assert h1 != h2


def test_get_by_handle_returns_bytes(test_user: User) -> None:
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    entry = media_staging.get_by_handle(handle)
    assert entry is not None
    user_id, url, content, mime = entry
    assert user_id == test_user.id
    assert url == "url-1"
    assert content == b"bytes"
    assert mime == "image/jpeg"


def test_get_by_handle_missing(test_user: User) -> None:
    assert media_staging.get_by_handle("media_missing") is None


def test_evict_by_handle(test_user: User) -> None:
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    assert media_staging.evict_by_handle(handle) is True
    assert media_staging.get_by_handle(handle) is None
    # Idempotent: second evict returns False because already gone.
    assert media_staging.evict_by_handle(handle) is False


def test_touch_extends_ttl(test_user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    # Jump forward to just before expiry, then touch.
    real_monotonic = time.monotonic
    future = real_monotonic() + media_staging.STAGING_TTL_SECONDS - 60
    monkeypatch.setattr(media_staging.time, "monotonic", lambda: future)
    assert media_staging.touch(handle) is True
    # Further jump past the ORIGINAL expiry; the touch must have kept it alive.
    further = real_monotonic() + media_staging.STAGING_TTL_SECONDS + 60
    monkeypatch.setattr(media_staging.time, "monotonic", lambda: further)
    assert media_staging.get_by_handle(handle) is not None


def test_touch_unknown_handle(test_user: User) -> None:
    assert media_staging.touch("media_missing") is False


def test_get_handle_for_roundtrip(test_user: User) -> None:
    handle = media_staging.stage(test_user.id, "url-xyz", b"b", "image/jpeg")
    assert handle is not None
    assert media_staging.get_handle_for(test_user.id, "url-xyz") == handle
    assert media_staging.get_handle_for(test_user.id, "missing") is None


# ---------------------------------------------------------------------------
# Pipeline gating tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_flag_off_preserves_vision_behavior(mock_vision: AsyncMock, test_user: User) -> None:
    """Regression: with the flag off, vision still runs unconditionally."""
    mock_vision.return_value = "Deck with weathering."
    result = await process_message_media("check this deck", [_make_media()], user_id=test_user.id)
    assert mock_vision.await_count == 1
    assert result.media_results[0].extracted_text == "Deck with weathering."


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_flag_on_skips_vision(
    mock_vision: AsyncMock, test_user: User, agent_native_on: None
) -> None:
    """Flag on: pipeline stages bytes and labels the context with a handle,
    but does not call the vision LLM."""
    media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    result = await process_message_media("hi", [_make_media("url-1")], user_id=test_user.id)
    assert mock_vision.await_count == 0
    # Context surfaces the handle so the agent knows what to call.
    handle = media_staging.get_handle_for(test_user.id, "url-1")
    assert handle is not None
    assert "call analyze_photo" in result.combined_context
    assert handle in result.combined_context


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_flag_on_empty_extracted_text(
    mock_vision: AsyncMock, test_user: User, agent_native_on: None
) -> None:
    """Flag on: ProcessedMedia.extracted_text is empty so nothing leaks into
    conversation history before the agent decides."""
    media_staging.stage(test_user.id, "url-2", b"b", "image/jpeg")
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
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
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
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
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
async def test_analyze_photo_wrong_user(test_user: User) -> None:
    """A handle minted for another user must not leak bytes across users."""
    handle = media_staging.stage("user-a", "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    tools = create_media_tools("user-b", "", {})
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)
    result = await analyze.function(handle=handle)
    assert result.is_error is True
    assert "not belong" in result.content


# ---------------------------------------------------------------------------
# discard_media tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_discard_media_evicts_handle(test_user: User) -> None:
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    tools = create_media_tools(test_user.id, "", {})
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)
    result = await discard.function(handle=handle, reason='user said "drop this"')
    assert result.is_error is False
    assert media_staging.get_by_handle(handle) is None


@pytest.mark.asyncio()
async def test_discard_media_idempotent(test_user: User) -> None:
    """A second discard on the same handle must not error — otherwise the
    agent gets stuck retrying."""
    handle = media_staging.stage(test_user.id, "url-1", b"bytes", "image/jpeg")
    assert handle is not None
    tools = create_media_tools(test_user.id, "", {})
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)
    r1 = await discard.function(handle=handle, reason='user said "drop"')
    r2 = await discard.function(handle=handle, reason='user said "drop"')
    assert r1.is_error is False
    assert r2.is_error is False
    assert "already discarded" in r2.content or "not staged" in r2.content


def test_reason_with_quoted_phrase_skips_ask() -> None:
    """The prompt-injection guard: quoted text in the reason unlocks auto-approve."""
    assert _reason_has_quoted_phrase('user said "don\'t save this"') is True
    assert _reason_has_quoted_phrase("user said 'skip this'") is True
    # Curly quotes from iOS keyboards should also count.
    assert _reason_has_quoted_phrase("user said \u201cdrop\u201d") is True


def test_reason_without_quoted_phrase_requires_ask() -> None:
    """No quoted text means an adversarial image may have talked the agent
    into the call; fall back to the approval prompt."""
    assert _reason_has_quoted_phrase("seemed unneeded") is False
    assert _reason_has_quoted_phrase("") is False
    # Single stray quote is not a pair.
    assert _reason_has_quoted_phrase('contains only one " char') is False


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
# Registry gating
# ---------------------------------------------------------------------------


def test_media_factory_returns_empty_when_flag_off(test_user: User) -> None:
    media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    ctx = ToolContext(user=test_user, downloaded_media=[_make_media("url-1")])
    assert _media_factory(ctx) == []


def test_media_factory_returns_empty_when_no_staged(test_user: User, agent_native_on: None) -> None:
    """Even with the flag on, no media = no tool surface (avoid prompt bloat)."""
    ctx = ToolContext(user=test_user, downloaded_media=[])
    assert _media_factory(ctx) == []


def test_media_factory_registers_tools_when_staged(test_user: User, agent_native_on: None) -> None:
    media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    ctx = ToolContext(user=test_user, downloaded_media=[_make_media("url-1")])
    tools = _media_factory(ctx)
    names = {t.name for t in tools}
    assert ToolName.ANALYZE_PHOTO in names
    assert ToolName.DISCARD_MEDIA in names


def test_media_factory_registers_when_staged_without_current_downloads(
    test_user: User, agent_native_on: None
) -> None:
    """Agent may call analyze_photo on a later turn that has no new attachments."""
    media_staging.stage(test_user.id, "url-1", b"b", "image/jpeg")
    ctx = ToolContext(user=test_user, downloaded_media=[])
    tools = _media_factory(ctx)
    assert len(tools) == 2


# ---------------------------------------------------------------------------
# Startup mutual-exclusion check
# ---------------------------------------------------------------------------


def test_rejects_dual_personal_storage_providers() -> None:
    s = Settings(
        dropbox_access_token="dbx-xxx",
        google_drive_credentials_json='{"type":"service_account"}',
    )
    with pytest.raises(RuntimeError, match="Two personal-storage backends"):
        validate_personal_storage_backend(s)


def test_allows_single_personal_storage_provider() -> None:
    s = Settings(dropbox_access_token="dbx-xxx")
    validate_personal_storage_backend(s)  # no raise

    s2 = Settings(google_drive_credentials_json='{"type":"service_account"}')
    validate_personal_storage_backend(s2)  # no raise

    s3 = Settings()  # neither set — local fallback
    validate_personal_storage_backend(s3)  # no raise


# ---------------------------------------------------------------------------
# Silences unused imports in simpler environments.
# ---------------------------------------------------------------------------

assert media_tools is not None
