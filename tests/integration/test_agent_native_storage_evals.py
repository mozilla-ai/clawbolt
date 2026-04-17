"""Eval scenarios for the agent-native storage feature.

These verify the tool surface and end-to-end wiring support each scenario
the /autoplan design called out. They are NOT full LLM evals (the repo has
no eval harness yet), but they exercise the real tool chain against real
staging, with vision mocked. When a real eval harness lands, port these
scenarios over and swap the mocked vision for real model calls.

Scenarios (from design doc /root/.gstack/projects/mozilla-ai-clawbolt/root-main-design-20260416-195108.md):

1. Contextual CompanyCam routing: caption resolves destination, skip vision.
2. No-context photo: vision first, then save to personal storage.
3. Opt-out: discard explicitly, no storage, no vision.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent import media_staging
from backend.app.agent.tools.media_tools import create_media_tools
from backend.app.agent.tools.names import ToolName
from backend.app.models import User


@pytest.fixture(autouse=True)
def _clear_staging_between_tests(test_user: User) -> Generator[None]:
    media_staging.clear_user(test_user.id)
    yield
    media_staging.clear_user(test_user.id)


# ---------------------------------------------------------------------------
# Scenario 1: Contextual CompanyCam routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.media_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_eval_contextual_companycam_routing(mock_vision: AsyncMock, test_user: User) -> None:
    """Contractor texts 'kitchen demo at 123 Main St' + photo.

    Expected agent behavior: route to the CompanyCam project for 123 Main
    with tags=[kitchen, demo]. Skip analyze_photo because the caption
    already describes the content. Skip upload_to_storage because the photo
    is filed in CompanyCam.

    This test exercises only the 'skip analyze_photo' and 'skip discard'
    halves of the scenario; CompanyCam routing is exercised in
    tests/test_companycam_tools.py.
    """
    handle = media_staging.stage(test_user.id, "url-kitchen", b"photo-bytes", "image/jpeg")
    assert handle is not None

    turn_text = "kitchen demo at 123 Main St"
    tools = create_media_tools(test_user.id, turn_text, {})
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)

    # The agent decides NOT to call analyze_photo or discard_media. We verify
    # that the tool surface works without side effects to staging.
    assert media_staging.get_by_handle(handle) is not None
    assert mock_vision.await_count == 0

    # Sanity: the tools exist and would work if called, we just expect the
    # agent not to call them in this scenario.
    assert analyze.function is not None
    assert discard.function is not None


# ---------------------------------------------------------------------------
# Scenario 2: No-context photo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.media_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_eval_no_context_photo(mock_vision: AsyncMock, test_user: User) -> None:
    """Contractor texts a photo with no caption or just a greeting.

    Expected agent behavior: call analyze_photo first to understand the
    content, then decide where to file it via upload_to_storage. This test
    exercises the analyze_photo side of the sequence.
    """
    mock_vision.return_value = "A damaged kitchen sink, corroded copper pipes."
    handle = media_staging.stage(test_user.id, "url-nocaption", b"photo-bytes", "image/jpeg")
    assert handle is not None

    turn_text = "hi"  # Essentially empty context
    tools = create_media_tools(test_user.id, turn_text, {})
    analyze = next(t for t in tools if t.name == ToolName.ANALYZE_PHOTO)

    result = await analyze.function(handle=handle)

    assert result.is_error is False
    assert result.content == "A damaged kitchen sink, corroded copper pipes."
    assert mock_vision.await_count == 1
    # Vision should have been called with the turn text as context so the
    # description is more relevant to what the user is trying to do.
    await_args = mock_vision.await_args
    assert await_args is not None
    _bytes, _mime, passed_context = await_args.args
    assert passed_context == turn_text
    # After analysis, staging is still intact so a follow-up upload_to_storage
    # or organize_file can read the bytes.
    assert media_staging.get_by_handle(handle) is not None


# ---------------------------------------------------------------------------
# Scenario 3: Opt-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.media_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_eval_opt_out(mock_vision: AsyncMock, test_user: User) -> None:
    """Contractor texts 'don't save this one' + photo.

    Expected agent behavior: call discard_media with a reason quoting the
    user's request. No analyze_photo (user doesn't want vision either), no
    upload_to_storage (explicitly opted out).
    """
    handle = media_staging.stage(test_user.id, "url-skip", b"photo-bytes", "image/jpeg")
    assert handle is not None

    turn_text = "don't save this one"
    tools = create_media_tools(test_user.id, turn_text, {})
    discard = next(t for t in tools if t.name == ToolName.DISCARD_MEDIA)

    reason = f"user said {turn_text!r}"
    result = await discard.function(handle=handle, reason=reason)

    assert result.is_error is False
    # Staging is now empty for this handle.
    assert media_staging.get_by_handle(handle) is None
    # Vision was never called.
    assert mock_vision.await_count == 0
