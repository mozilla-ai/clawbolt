import datetime
import json

import pytest

from backend.app.agent.context import (
    get_or_create_conversation,
    load_conversation_history,
)
from backend.app.agent.file_store import SessionState, StoredMessage
from backend.app.agent.messages import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from backend.app.models import User


@pytest.fixture()
def conversation(test_user: User) -> SessionState:
    import asyncio

    from backend.app.agent.session_db import get_session_store

    store = get_session_store(test_user.id)
    session, _is_new = asyncio.get_event_loop().run_until_complete(store.get_or_create_session())
    return session


@pytest.mark.asyncio()
async def test_load_history_chronological_order(
    test_user: User,
    conversation: SessionState,
) -> None:
    """History should be in chronological order."""
    for i in range(3):
        conversation.messages.append(
            StoredMessage(
                direction="inbound" if i % 2 == 0 else "outbound",
                body=f"Message {i}",
                seq=i + 1,
            )
        )
    # Add the "current" message
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=4))

    history = await load_conversation_history(conversation)
    # Should exclude the current (most recent) message
    assert len(history) == 3
    assert history[0].content == "Message 0"
    assert history[1].content == "Message 1"
    assert history[2].content == "Message 2"


@pytest.mark.asyncio()
async def test_load_history_roles(
    conversation: SessionState,
) -> None:
    """Inbound = user, outbound = assistant."""
    conversation.messages.append(StoredMessage(direction="inbound", body="Hi", seq=1))
    conversation.messages.append(StoredMessage(direction="outbound", body="Hello!", seq=2))
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)
    assert isinstance(history[0], UserMessage)
    assert isinstance(history[1], AssistantMessage)


@pytest.mark.asyncio()
async def test_load_history_soft_limit(
    conversation: SessionState,
) -> None:
    """The soft safety-net limit should still cap loaded messages."""
    for i in range(10):
        conversation.messages.append(StoredMessage(direction="inbound", body=f"Msg {i}", seq=i + 1))

    # With the soft limit at 5, only 5 most recent are loaded (minus current = 4)
    history = await load_conversation_history(conversation, limit=5)
    assert len(history) == 4

    # With the default 500 limit, all messages should load (minus current = 9)
    history_all = await load_conversation_history(conversation, limit=500)
    assert len(history_all) == 9


@pytest.mark.asyncio()
async def test_load_history_prefers_processed_context(
    conversation: SessionState,
) -> None:
    """History should use processed_context over raw body when available."""
    conversation.messages.append(
        StoredMessage(
            direction="inbound",
            body="Check this photo",
            processed_context="[Text message]: 'Check this photo'\n[Photo 1]: A damaged deck railing",
            seq=1,
        )
    )
    # Add current message
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=2))

    history = await load_conversation_history(conversation)
    content = history[0].content
    assert content is not None
    assert "damaged deck railing" in content


@pytest.mark.asyncio()
async def test_load_history_empty_conversation(
    conversation: SessionState,
) -> None:
    """Empty conversation should return empty history."""
    history = await load_conversation_history(conversation)
    assert history == []


@pytest.mark.asyncio()
async def test_load_history_single_message(
    conversation: SessionState,
) -> None:
    """Single message (current) should return empty history."""
    conversation.messages.append(StoredMessage(direction="inbound", body="Only msg", seq=1))

    history = await load_conversation_history(conversation)
    assert history == []


@pytest.mark.asyncio()
async def test_get_or_create_conversation_new(
    test_user: User,
) -> None:
    """Should create a new conversation when none exists."""
    conv, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is True
    assert conv.user_id == test_user.id
    assert conv.is_active is True


@pytest.mark.asyncio()
async def test_get_or_create_conversation_existing_active(
    test_user: User,
    conversation: SessionState,
) -> None:
    """Should return existing active conversation."""
    # The conversation fixture already created a recent session on disk
    conv, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is False
    assert conv.session_id == conversation.session_id


@pytest.mark.asyncio()
async def test_get_or_create_conversation_reuses_old_session(
    test_user: User,
) -> None:
    """Should reuse existing session regardless of age (persistent model)."""

    # Create an old conversation directly in the database
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
    old_session_id = "old-conv"

    import backend.app.database as _db_module
    from backend.app.models import ChatSession

    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id=old_session_id,
            user_id=test_user.id,
            is_active=True,
            channel="",
            last_compacted_seq=0,
            created_at=old_time,
            last_message_at=old_time,
        )
        db.add(cs)
        db.commit()
    finally:
        db.close()

    conv, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is False
    assert conv.session_id == old_session_id


@pytest.mark.asyncio()
async def test_get_or_create_conversation_with_external_session_id(
    test_user: User,
) -> None:
    """New conversation should store external session ID."""
    conv, is_new = await get_or_create_conversation(
        test_user.id, external_session_id="session_abc123"
    )
    assert is_new is True
    # SessionState stores external_session_id if available
    assert conv.session_id is not None


@pytest.mark.asyncio()
async def test_get_or_create_conversation_force_new(
    test_user: User,
) -> None:
    """force_new=True should always create a new session."""
    conv1, _ = await get_or_create_conversation(test_user.id)

    conv2, is_new = await get_or_create_conversation(test_user.id, force_new=True)
    assert is_new is True
    assert conv2.session_id != conv1.session_id

    # Without force_new, should reuse the newest session
    conv3, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is False
    assert conv3.session_id == conv2.session_id


def test_webhook_uses_canonical_get_or_create_conversation() -> None:
    """Webhook should use context.get_or_create_conversation, not a local duplicate."""
    from backend.app.channels import telegram

    # The local _get_or_create_conversation should no longer exist
    assert not hasattr(telegram, "_get_or_create_conversation")


# ---------------------------------------------------------------------------
# Tool interaction persistence tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_load_history_reconstructs_tool_interactions(
    conversation: SessionState,
) -> None:
    """Outbound messages with tool_interactions_json should expand to full sequence."""
    # Inbound message
    conversation.messages.append(StoredMessage(direction="inbound", body="Save my rate", seq=1))

    # Outbound with tool interactions
    tool_data = [
        {
            "tool_call_id": "call_abc",
            "name": "save_fact",
            "args": {"key": "rate", "value": "$85/hr"},
            "result": "Saved: rate = $85/hr",
            "is_error": False,
        }
    ]
    conversation.messages.append(
        StoredMessage(
            direction="outbound",
            body="I saved your rate.",
            tool_interactions_json=json.dumps(tool_data),
            seq=2,
        )
    )

    # Current message (will be excluded)
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)

    # Should be: UserMessage, AssistantMessage(tool_calls), ToolResultMessage, AssistantMessage
    assert len(history) == 4
    assert isinstance(history[0], UserMessage)
    assert history[0].content == "Save my rate"

    assert isinstance(history[1], AssistantMessage)
    assert len(history[1].tool_calls) == 1
    assert history[1].tool_calls[0].name == "save_fact"
    assert history[1].tool_calls[0].id == "call_abc"

    assert isinstance(history[2], ToolResultMessage)
    assert history[2].tool_call_id == "call_abc"
    assert "Saved: rate" in history[2].content

    assert isinstance(history[3], AssistantMessage)
    assert history[3].content == "I saved your rate."


@pytest.mark.asyncio()
async def test_load_history_without_tool_interactions(
    conversation: SessionState,
) -> None:
    """Outbound messages without tool_interactions_json load as flat text."""
    conversation.messages.append(StoredMessage(direction="inbound", body="Hello", seq=1))
    conversation.messages.append(
        StoredMessage(
            direction="outbound",
            body="Hi there!",
            tool_interactions_json="",
            seq=2,
        )
    )
    # Current
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)
    assert len(history) == 2
    assert isinstance(history[0], UserMessage)
    assert isinstance(history[1], AssistantMessage)
    assert history[1].content == "Hi there!"
    assert history[1].tool_calls == []


@pytest.mark.asyncio()
async def test_load_history_multiple_tool_calls_in_one_turn(
    conversation: SessionState,
) -> None:
    """Multiple tool calls in a single turn should all be reconstructed."""
    conversation.messages.append(StoredMessage(direction="inbound", body="Save two facts", seq=1))
    tool_data = [
        {
            "tool_call_id": "call_1",
            "name": "save_fact",
            "args": {"key": "rate", "value": "$85/hr"},
            "result": "Saved: rate = $85/hr",
            "is_error": False,
        },
        {
            "tool_call_id": "call_2",
            "name": "save_fact",
            "args": {"key": "trade", "value": "plumber"},
            "result": "Saved: trade = plumber",
            "is_error": False,
        },
    ]
    conversation.messages.append(
        StoredMessage(
            direction="outbound",
            body="Done!",
            tool_interactions_json=json.dumps(tool_data),
            seq=2,
        )
    )
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)

    # UserMessage, AssistantMessage(2 tool_calls), ToolResult, ToolResult, AssistantMessage
    assert len(history) == 5
    assert isinstance(history[1], AssistantMessage)
    assert len(history[1].tool_calls) == 2
    assert isinstance(history[2], ToolResultMessage)
    assert isinstance(history[3], ToolResultMessage)
    assert isinstance(history[4], AssistantMessage)
    assert history[4].content == "Done!"


@pytest.mark.asyncio()
async def test_load_history_malformed_tool_json_falls_back_to_flat(
    conversation: SessionState,
) -> None:
    """Malformed tool_interactions_json should fall back to flat AssistantMessage."""
    conversation.messages.append(StoredMessage(direction="inbound", body="Hello", seq=1))
    conversation.messages.append(
        StoredMessage(
            direction="outbound",
            body="Reply text",
            tool_interactions_json="not valid json{{{",
            seq=2,
        )
    )
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)
    assert len(history) == 2
    assert isinstance(history[1], AssistantMessage)
    assert history[1].content == "Reply text"
    assert history[1].tool_calls == []


# ---------------------------------------------------------------------------
# Regression tests for issue #1049: poisoned approval prompts in history
# trained the LLM to mimic the format and produce fake permission asks.
# ---------------------------------------------------------------------------


_APPROVAL_PROMPT = (
    "I'd like to: Create Estimate in QuickBooks\n\n"
    "Reply yes or no (always/never to remember your choice)"
)


@pytest.mark.asyncio()
async def test_load_history_filters_persisted_approval_prompts(
    conversation: SessionState,
) -> None:
    """Outbound messages that are clearly approval prompts (canonical
    trailer, no tool_interactions) must not appear in the LLM-side
    history. Persisted prompts in past turns trained the LLM to mimic
    the format as plain prose, breaking the approval gate."""
    conversation.messages.append(
        StoredMessage(direction="inbound", body="Create that estimate", seq=1)
    )
    conversation.messages.append(StoredMessage(direction="outbound", body=_APPROVAL_PROMPT, seq=2))
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)
    assert len(history) == 1
    assert isinstance(history[0], UserMessage)
    assert history[0].content == "Create that estimate"
    assert all(
        "Reply yes or no" not in (m.content or "")
        for m in history
        if isinstance(m, AssistantMessage)
    )


@pytest.mark.asyncio()
async def test_load_history_filters_orphan_approval_reply_following_prompt(
    conversation: SessionState,
) -> None:
    """An inbound that parses as a fast-path approval response and
    immediately follows a (just-filtered) approval prompt is also
    dropped. Otherwise the orphan "Yes" floats in history with no
    antecedent and risks similar pattern-matching by the LLM."""
    conversation.messages.append(
        StoredMessage(direction="inbound", body="Create that estimate", seq=1)
    )
    conversation.messages.append(StoredMessage(direction="outbound", body=_APPROVAL_PROMPT, seq=2))
    conversation.messages.append(StoredMessage(direction="inbound", body="Always", seq=3))
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=4))

    history = await load_conversation_history(conversation)
    # Only the original "Create that estimate" inbound survives.
    assert len(history) == 1
    assert isinstance(history[0], UserMessage)
    assert history[0].content == "Create that estimate"


@pytest.mark.asyncio()
async def test_load_history_keeps_yes_unrelated_to_approval_prompt(
    conversation: SessionState,
) -> None:
    """A user "Yes" that does NOT immediately follow a stripped
    approval prompt is preserved. Filtering it would silently drop
    legitimate one-word replies in normal conversation."""
    conversation.messages.append(StoredMessage(direction="inbound", body="Should I do X?", seq=1))
    conversation.messages.append(StoredMessage(direction="outbound", body="Want me to?", seq=2))
    conversation.messages.append(StoredMessage(direction="inbound", body="Yes", seq=3))
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=4))

    history = await load_conversation_history(conversation)
    assert len(history) == 3
    assert isinstance(history[2], UserMessage)
    assert history[2].content == "Yes"


@pytest.mark.asyncio()
async def test_load_history_keeps_assistant_reply_that_happens_to_mention_yes_no(
    conversation: SessionState,
) -> None:
    """An ordinary assistant reply that happens to mention "yes or no"
    in prose but doesn't end with the canonical trailer is preserved."""
    conversation.messages.append(
        StoredMessage(direction="inbound", body="What's your policy?", seq=1)
    )
    conversation.messages.append(
        StoredMessage(
            direction="outbound",
            body=(
                "Some questions are yes-or-no, others need more context. "
                "Tell me which kind you have."
            ),
            seq=2,
        )
    )
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=3))

    history = await load_conversation_history(conversation)
    assert len(history) == 2
    assert isinstance(history[1], AssistantMessage)
    assert "yes-or-no" in (history[1].content or "")


@pytest.mark.asyncio()
async def test_load_history_filters_llm_generated_fake_approval_prompt(
    conversation: SessionState,
) -> None:
    """When the LLM produces a fake approval prompt as plain prose
    (the bug from #1049), it gets persisted as outbound with no
    tool_interactions. The load-time filter must catch these too so
    the cycle stops on subsequent turns."""
    fake_prompt = (
        "I'd like to: Create Estimate in QuickBooks\n\n"
        "Reply yes or no (always/never to remember your choice)"
    )
    conversation.messages.append(StoredMessage(direction="inbound", body="estimate?", seq=1))
    conversation.messages.append(
        StoredMessage(
            direction="outbound",
            body=fake_prompt,
            tool_interactions_json="",
            seq=2,
        )
    )
    conversation.messages.append(StoredMessage(direction="inbound", body="Yes", seq=3))
    conversation.messages.append(StoredMessage(direction="inbound", body="Current", seq=4))

    history = await load_conversation_history(conversation)
    # Only the original user prompt survives; fake "I'd like to:" and the
    # orphan "Yes" reply are both stripped.
    assert len(history) == 1
    assert isinstance(history[0], UserMessage)
    assert history[0].content == "estimate?"
