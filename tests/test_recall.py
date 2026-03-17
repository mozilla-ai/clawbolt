"""Tests for memory recall behavior in system prompt and context."""

from unittest.mock import patch

import pytest

from backend.app.agent.file_store import SessionState, StoredMessage
from backend.app.agent.memory import build_memory_context
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.router import handle_inbound_message
from backend.app.models import User
from tests.mocks.llm import make_text_response


@pytest.fixture()
def session(test_user: User) -> SessionState:
    return SessionState(
        session_id="test-session",
        user_id=test_user.id,
        messages=[],
        is_active=True,
    )


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_system_prompt_includes_recall_guidance(
    mock_amessages: object,
    test_user: User,
    session: SessionState,
) -> None:
    """System prompt should include recall behavior guidance."""
    msg = StoredMessage(
        direction="inbound",
        body="What do you know about my rates?",
        seq=1,
    )
    session.messages.append(msg)

    mock_amessages.return_value = make_text_response("Let me check my memory.")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=session,
        message=msg,
        media_urls=[],
        channel="telegram",
    )

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["system"]
    assert "Recall Behavior" in system_prompt
    assert "don't make things up" in system_prompt


@pytest.mark.asyncio()
async def test_build_memory_context_includes_memory_content(
    test_user: User,
) -> None:
    """build_memory_context should include MEMORY.md content."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Pricing\n- Hourly rate: $75/hour for general work")

    context = await build_memory_context(test_user.id)
    assert "$75/hour" in context


@pytest.mark.asyncio()
async def test_build_memory_context_empty(test_user: User) -> None:
    """build_memory_context returns empty string when no memory exists."""
    context = await build_memory_context(test_user.id)
    assert context == ""
