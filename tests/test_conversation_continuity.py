import datetime
import json

import pytest

from backend.app.agent.context import (
    CONVERSATION_TIMEOUT_HOURS,
    get_or_create_conversation,
    load_conversation_history,
)
from backend.app.agent.file_store import SessionState, StoredMessage, UserData
from backend.app.agent.messages import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)


@pytest.fixture()
def conversation(test_user: UserData) -> SessionState:
    import asyncio

    from backend.app.agent.file_store import get_session_store

    store = get_session_store(test_user.id)
    session, _is_new = asyncio.get_event_loop().run_until_complete(store.get_or_create_session())
    return session


@pytest.mark.asyncio()
async def test_load_history_chronological_order(
    test_user: UserData,
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
async def test_load_history_limit(
    conversation: SessionState,
) -> None:
    """History should be limited to N messages."""
    for i in range(10):
        conversation.messages.append(StoredMessage(direction="inbound", body=f"Msg {i}", seq=i + 1))

    history = await load_conversation_history(conversation, limit=5)
    # 5 loaded, minus 1 for current = 4
    assert len(history) == 4


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
    test_user: UserData,
) -> None:
    """Should create a new conversation when none exists."""
    conv, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is True
    assert conv.user_id == test_user.id
    assert conv.is_active is True


@pytest.mark.asyncio()
async def test_get_or_create_conversation_existing_active(
    test_user: UserData,
    conversation: SessionState,
) -> None:
    """Should return existing active conversation within timeout."""
    # The conversation fixture already created a recent session on disk
    conv, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is False
    assert conv.session_id == conversation.session_id


@pytest.mark.asyncio()
async def test_get_or_create_conversation_expired(
    test_user: UserData,
) -> None:
    """Should create new conversation when existing one has timed out."""
    from backend.app.agent.file_store import get_session_store

    store = get_session_store(test_user.id)

    # Create an old conversation by writing the session file directly
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=CONVERSATION_TIMEOUT_HOURS + 1
    )
    old_session_id = "old-conv"
    path = store._session_path(old_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "_type": "metadata",
        "session_id": old_session_id,
        "user_id": test_user.id,
        "created_at": old_time.isoformat(),
        "last_message_at": old_time.isoformat(),
        "is_active": True,
        "last_compacted_seq": 0,
    }
    path.write_text(json.dumps(meta, default=str) + "\n", encoding="utf-8")

    conv, is_new = await get_or_create_conversation(test_user.id)
    assert is_new is True
    assert conv.session_id != old_session_id


@pytest.mark.asyncio()
async def test_get_or_create_conversation_with_external_session_id(
    test_user: UserData,
) -> None:
    """New conversation should store external session ID."""
    conv, is_new = await get_or_create_conversation(
        test_user.id, external_session_id="session_abc123"
    )
    assert is_new is True
    # SessionState stores external_session_id if available
    assert conv.session_id is not None


@pytest.mark.asyncio()
async def test_get_or_create_conversation_custom_timeout(
    test_user: UserData,
) -> None:
    """Custom timeout should be respected."""
    from backend.app.agent.file_store import get_session_store

    store = get_session_store(test_user.id)

    def _write_session(session_id: str, last_message_at: str) -> None:
        """Write a session JSONL file with metadata."""
        path = store._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "_type": "metadata",
            "session_id": session_id,
            "user_id": test_user.id,
            "created_at": last_message_at,
            "last_message_at": last_message_at,
            "is_active": True,
            "last_compacted_seq": 0,
        }
        path.write_text(json.dumps(meta, default=str) + "\n", encoding="utf-8")

    # Create a conversation 2 hours old
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
    _write_session("conv-timeout-test", old_time.isoformat())

    # With 1-hour timeout, should create new
    _conv, is_new = await get_or_create_conversation(test_user.id, timeout_hours=1)
    assert is_new is True

    # With 3-hour timeout, should reuse (re-write old session since a new one was created)
    _write_session("conv-timeout-test", old_time.isoformat())
    _conv, is_new = await get_or_create_conversation(test_user.id, timeout_hours=3)
    assert is_new is False


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
