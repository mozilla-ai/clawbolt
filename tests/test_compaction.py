"""Tests for session compaction (extracting durable facts from aging messages)."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    _format_messages_for_compaction,
    _parse_compaction_response,
    compact_session,
)
from backend.app.agent.context import (
    _consolidate_previous_session,
    get_or_create_conversation,
    load_conversation_history,
)
from backend.app.agent.file_store import (
    SessionState,
    StoredMessage,
    UserData,
    get_memory_store,
    get_session_store,
    get_user_store,
)
from backend.app.agent.memory import get_all_memories
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.enums import MessageDirection
from tests.mocks.llm import make_text_response


@pytest.fixture()
def session(test_user: UserData) -> SessionState:
    """Create a SessionState for the test user."""
    return SessionState(
        session_id="test-session",
        user_id=test_user.id,
        messages=[],
        is_active=True,
    )


def _add_messages(session: SessionState, count: int) -> None:
    """Add the given number of test messages to a session."""
    for i in range(count):
        session.messages.append(
            StoredMessage(
                direction="inbound" if i % 2 == 0 else "outbound",
                body=f"Message {i}",
                seq=i + 1,
            )
        )


# --- _format_messages_for_compaction tests ---


def test_format_messages_basic() -> None:
    """Format should produce readable User/Assistant lines."""
    messages: list[AgentMessage] = [
        UserMessage(content="I charge $45/sqft for composite decks"),
        AssistantMessage(content="Got it, I'll remember that pricing."),
        UserMessage(content="My supplier is ABC Lumber on 5th Ave"),
    ]
    result = _format_messages_for_compaction(messages)
    assert "User: I charge $45/sqft" in result
    assert "Assistant: Got it" in result
    assert "User: My supplier is ABC Lumber" in result


def test_format_messages_empty() -> None:
    """Empty message list should produce empty string."""
    assert _format_messages_for_compaction([]) == ""


def test_format_messages_skips_empty_assistant() -> None:
    """Assistant messages with no content should be skipped."""
    messages: list[AgentMessage] = [
        UserMessage(content="Hello"),
        AssistantMessage(content=None),
        UserMessage(content="World"),
    ]
    result = _format_messages_for_compaction(messages)
    assert "Assistant:" not in result
    assert "User: Hello" in result
    assert "User: World" in result


# --- _parse_compaction_response tests ---


def test_parse_valid_json_array() -> None:
    """Should parse a valid JSON array of fact objects (legacy format)."""
    raw = json.dumps(
        [
            {"key": "deck_pricing", "value": "$45/sqft composite", "category": "pricing"},
            {"key": "supplier_name", "value": "ABC Lumber", "category": "supplier"},
        ]
    )
    facts, summary = _parse_compaction_response(raw)
    assert len(facts) == 2
    assert facts[0]["key"] == "deck_pricing"
    assert facts[0]["value"] == "$45/sqft composite"
    assert facts[0]["category"] == "pricing"
    assert facts[1]["key"] == "supplier_name"
    assert summary == ""


def test_parse_new_object_format() -> None:
    """Should parse the new format with facts and summary."""
    raw = json.dumps(
        {
            "facts": [{"key": "rate", "value": "$85/hr", "category": "pricing"}],
            "summary": "[TIMESTAMP] Discussed pricing for kitchen remodel.",
        }
    )
    facts, summary = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "rate"
    assert summary == "[TIMESTAMP] Discussed pricing for kitchen remodel."


def test_parse_new_format_empty_facts_and_summary() -> None:
    """Should handle new format with empty facts and summary."""
    raw = json.dumps({"facts": [], "summary": ""})
    facts, summary = _parse_compaction_response(raw)
    assert facts == []
    assert summary == ""


def test_parse_markdown_fenced_json() -> None:
    """Should handle markdown code fences around JSON."""
    raw = '```json\n[{"key": "rate", "value": "$50/hr", "category": "pricing"}]\n```'
    facts, summary = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "rate"
    assert summary == ""


def test_parse_markdown_fenced_new_format() -> None:
    """Should handle markdown code fences around the new object format."""
    inner = json.dumps(
        {
            "facts": [{"key": "k", "value": "v", "category": "general"}],
            "summary": "A summary.",
        }
    )
    raw = f"```json\n{inner}\n```"
    facts, summary = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert summary == "A summary."


def test_parse_empty_array() -> None:
    """Empty JSON array should return empty list."""
    facts, summary = _parse_compaction_response("[]")
    assert facts == []
    assert summary == ""


def test_parse_invalid_json() -> None:
    """Invalid JSON should return empty list without raising."""
    facts, summary = _parse_compaction_response("not json at all")
    assert facts == []
    assert summary == ""


def test_parse_non_array_json_without_facts_key() -> None:
    """Object without 'facts' key should return empty list."""
    facts, summary = _parse_compaction_response('{"key": "val"}')
    assert facts == []
    assert summary == ""


def test_parse_skips_items_without_key() -> None:
    """Items missing 'key' should be skipped."""
    raw = json.dumps(
        [
            {"value": "no key here", "category": "general"},
            {"key": "good_fact", "value": "this is valid", "category": "general"},
        ]
    )
    facts, _ = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "good_fact"


def test_parse_skips_items_without_value() -> None:
    """Items missing 'value' should be skipped."""
    raw = json.dumps(
        [
            {"key": "empty_val", "value": "", "category": "general"},
            {"key": "good", "value": "present", "category": "general"},
        ]
    )
    facts, _ = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "good"


def test_parse_normalizes_invalid_category() -> None:
    """Unknown categories should be normalized to 'general'."""
    raw = json.dumps(
        [
            {"key": "fact1", "value": "something", "category": "unknown_cat"},
        ]
    )
    facts, _ = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["category"] == "general"


def test_parse_skips_non_dict_items() -> None:
    """Non-dict items in the array should be skipped."""
    raw = json.dumps(
        [
            "just a string",
            42,
            {"key": "valid", "value": "yes", "category": "general"},
        ]
    )
    facts, _ = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "valid"


# --- compact_session tests ---


@pytest.mark.asyncio()
async def test_compact_session_extracts_and_saves_facts(
    test_user: UserData,
) -> None:
    """compact_session should call LLM and save extracted facts to memory."""
    llm_response_content = json.dumps(
        [
            {"key": "deck_rate", "value": "$45/sqft for composite", "category": "pricing"},
            {"key": "client_smith_phone", "value": "555-0123", "category": "client"},
        ]
    )

    mock_response = make_text_response(llm_response_content)

    messages: list[AgentMessage] = [
        UserMessage(content="I usually charge $45 per square foot for composite decks"),
        AssistantMessage(content="Got it, I'll remember that."),
        UserMessage(content="Oh and Mr. Smith's number is 555-0123"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        saved, max_seq = await compact_session(test_user.id, messages)

    assert len(saved) == 2
    assert saved[0]["key"] == "deck_rate"
    assert saved[1]["key"] == "client_smith_phone"
    # No max_message_seq passed, so should be None
    assert max_seq is None

    # Verify facts were persisted
    memories = await get_all_memories(test_user.id)
    assert len(memories) == 2
    keys = {m.key for m in memories}
    assert "deck_rate" in keys
    assert "client_smith_phone" in keys

    # Verify confidence is set to 0.8 for compacted facts
    for m in memories:
        assert m.confidence == 0.8

    # Verify LLM was called with the system prompt
    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("system") == COMPACTION_SYSTEM_PROMPT


@pytest.mark.asyncio()
async def test_compact_session_returns_max_message_seq(
    test_user: UserData,
) -> None:
    """compact_session should return the max_message_seq when provided."""
    mock_response = make_text_response(
        json.dumps([{"key": "fact", "value": "val", "category": "general"}])
    )

    messages: list[AgentMessage] = [UserMessage(content="test")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved, max_seq = await compact_session(test_user.id, messages, max_message_seq=42)

    assert len(saved) == 1
    assert max_seq == 42


@pytest.mark.asyncio()
async def test_compact_session_empty_messages(
    test_user: UserData,
) -> None:
    """compact_session with no messages should return empty list without LLM call."""
    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        saved, max_seq = await compact_session(test_user.id, [])

    assert saved == []
    assert max_seq is None
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_disabled(test_user: UserData) -> None:
    """compact_session should skip when compaction_enabled is False."""
    messages: list[AgentMessage] = [UserMessage(content="Some content")]

    with (
        patch("backend.app.agent.compaction.settings") as mock_settings,
        patch("backend.app.agent.compaction.amessages") as mock_llm,
    ):
        mock_settings.compaction_enabled = False
        saved, max_seq = await compact_session(test_user.id, messages)

    assert saved == []
    assert max_seq is None
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_llm_failure_returns_empty(
    test_user: UserData,
) -> None:
    """compact_session should return empty list if LLM call fails."""
    messages: list[AgentMessage] = [UserMessage(content="Some content")]

    with patch(
        "backend.app.agent.compaction.amessages",
        side_effect=Exception("LLM unavailable"),
    ):
        saved, max_seq = await compact_session(test_user.id, messages)

    assert saved == []
    assert max_seq is None


@pytest.mark.asyncio()
async def test_compact_session_invalid_llm_response(
    test_user: UserData,
) -> None:
    """compact_session should handle unparseable LLM responses gracefully."""
    mock_response = make_text_response("Sorry, I can't do that.")

    messages: list[AgentMessage] = [UserMessage(content="Some content")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved, max_seq = await compact_session(test_user.id, messages)

    assert saved == []
    assert max_seq is None


@pytest.mark.asyncio()
async def test_compact_session_no_durable_facts(
    test_user: UserData,
) -> None:
    """compact_session should handle LLM returning empty array (no facts)."""
    mock_response = make_text_response("[]")

    messages: list[AgentMessage] = [
        UserMessage(content="Hey there"),
        AssistantMessage(content="Hello! How can I help?"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved, max_seq = await compact_session(test_user.id, messages)

    assert saved == []
    assert max_seq is None
    memories = await get_all_memories(test_user.id)
    assert len(memories) == 0


@pytest.mark.asyncio()
async def test_compact_session_uses_configured_model(
    test_user: UserData,
) -> None:
    """compact_session should use compaction_model/provider when configured."""
    mock_response = make_text_response("[]")

    messages: list[AgentMessage] = [UserMessage(content="test")]

    with (
        patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm,
        patch("backend.app.agent.compaction.settings") as mock_settings,
    ):
        mock_settings.compaction_enabled = True
        mock_settings.compaction_model = "test-compact-model"
        mock_settings.compaction_provider = "test-provider"
        mock_settings.compaction_max_tokens = 300
        mock_settings.llm_model = "test-model"
        mock_settings.llm_provider = "test-provider"
        mock_settings.llm_api_base = None
        await compact_session(test_user.id, messages)

    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "test-compact-model"


@pytest.mark.asyncio()
async def test_compact_session_falls_back_to_llm_model(
    test_user: UserData,
) -> None:
    """compact_session should fall back to llm_model when compaction_model is empty."""
    mock_response = make_text_response("[]")

    messages: list[AgentMessage] = [UserMessage(content="test")]

    with (
        patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm,
        patch("backend.app.agent.compaction.settings") as mock_settings,
    ):
        mock_settings.compaction_enabled = True
        mock_settings.compaction_model = ""
        mock_settings.compaction_provider = ""
        mock_settings.compaction_max_tokens = 500
        mock_settings.llm_model = "test-model"
        mock_settings.llm_provider = "test-provider"
        mock_settings.llm_api_base = None
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "test-model"
    assert call_kwargs.kwargs.get("provider") == "test-provider"


# --- Integration: load_conversation_history with compaction ---


@pytest.mark.asyncio()
async def test_load_history_triggers_compaction_when_full(
    test_user: UserData,
    session: SessionState,
) -> None:
    """When history exceeds limit, compaction should run on trimmed messages."""
    _add_messages(session, 8)

    llm_response_content = json.dumps(
        [
            {"key": "fact_from_compaction", "value": "extracted", "category": "general"},
        ]
    )
    mock_response = make_text_response(llm_response_content)

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        # Use limit=5, so 3 messages are trimmed (8 total, 5 loaded, minus current = 4 history)
        history = await load_conversation_history(session, limit=5, user_id=test_user.id)
        # Allow the background compaction task to complete
        await asyncio.sleep(0.1)

    # History should have 4 messages (5 loaded minus 1 current)
    assert len(history) == 4

    # Compacted fact should have been saved
    memories = await get_all_memories(test_user.id)
    assert len(memories) == 1
    assert memories[0].key == "fact_from_compaction"


@pytest.mark.asyncio()
async def test_load_history_updates_last_compacted_seq(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Compaction should update last_compacted_seq on the session."""
    _add_messages(session, 8)

    mock_response = make_text_response(
        json.dumps([{"key": "f", "value": "v", "category": "general"}])
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await load_conversation_history(session, limit=5, user_id=test_user.id)
        await asyncio.sleep(0.1)

    # The trimmed messages are the first 3 (8 total - 5 limit).
    # Their seqs are 1, 2, 3, so the max compacted seq should be 3.
    assert session.last_compacted_seq > 0


@pytest.mark.asyncio()
async def test_load_history_skips_already_compacted_messages(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Messages already compacted should not be re-compacted."""
    _add_messages(session, 8)

    # Simulate that the first 2 messages were already compacted
    session.last_compacted_seq = 2

    mock_response = make_text_response(
        json.dumps([{"key": "new_fact", "value": "from_remaining", "category": "general"}])
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await load_conversation_history(session, limit=5, user_id=test_user.id)
        await asyncio.sleep(0.1)

    # LLM should have been called with only the 1 un-compacted trimmed message
    # (3 trimmed total, 2 already compacted = 1 new)
    mock_llm.assert_called_once()
    call_messages = mock_llm.call_args.kwargs.get("messages") or mock_llm.call_args[1].get(
        "messages"
    )
    # System prompt is now passed via 'system' kwarg, user content is messages[0]
    user_content = call_messages[0]["content"]
    assert "Message 2" in user_content
    assert "Message 0" not in user_content
    assert "Message 1" not in user_content


@pytest.mark.asyncio()
async def test_load_history_no_compaction_when_all_already_compacted(
    test_user: UserData,
    session: SessionState,
) -> None:
    """When all trimmed messages are already compacted, no LLM call should happen."""
    _add_messages(session, 8)

    # Mark all trimmed messages as already compacted (first 3 are trimmed with limit=5)
    session.last_compacted_seq = 3

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        await load_conversation_history(session, limit=5, user_id=test_user.id)
        await asyncio.sleep(0.1)

    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_load_history_no_compaction_when_under_limit(
    test_user: UserData,
    session: SessionState,
) -> None:
    """When history is under limit, no compaction should occur."""
    _add_messages(session, 3)

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        history = await load_conversation_history(session, limit=20, user_id=test_user.id)

    # Should not call LLM since we're under the limit
    mock_llm.assert_not_called()
    # 3 messages, minus current = 2
    assert len(history) == 2


@pytest.mark.asyncio()
async def test_load_history_no_compaction_without_user_id(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Without user_id, compaction should not run even at limit."""
    _add_messages(session, 8)

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        history = await load_conversation_history(session, limit=5)

    mock_llm.assert_not_called()
    assert len(history) == 4


@pytest.mark.asyncio()
async def test_load_history_compaction_failure_does_not_break_history(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Compaction failure should not prevent history from loading."""
    _add_messages(session, 8)

    with patch(
        "backend.app.agent.compaction.amessages",
        side_effect=Exception("LLM down"),
    ):
        history = await load_conversation_history(session, limit=5, user_id=test_user.id)
        # Allow the background task to complete (and fail gracefully)
        await asyncio.sleep(0.1)

    # History should still load correctly despite compaction failure
    assert len(history) == 4


@pytest.mark.asyncio()
async def test_compaction_runs_in_background_not_blocking(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Compaction should run as a background task, not blocking history loading."""
    _add_messages(session, 8)

    compaction_started = asyncio.Event()
    compaction_proceed = asyncio.Event()

    async def slow_compact(
        user_id: int,
        trimmed_messages: list[object],
        max_message_seq: int | None = None,
    ) -> tuple[list[dict[str, str]], int | None]:
        compaction_started.set()
        await compaction_proceed.wait()
        return [], max_message_seq

    with patch("backend.app.agent.context.compact_session", side_effect=slow_compact):
        history = await load_conversation_history(session, limit=5, user_id=test_user.id)

        # History should be returned immediately, even though compaction hasn't finished
        assert len(history) == 4

        # Compaction task should have started
        await asyncio.sleep(0.05)
        assert compaction_started.is_set()

        # Let compaction finish
        compaction_proceed.set()
        await asyncio.sleep(0.05)


# --- compact_session HISTORY.md tests ---


@pytest.mark.asyncio()
async def test_compact_session_appends_history(test_user: UserData) -> None:
    """compact_session should write summary to HISTORY.md."""
    llm_response_text = json.dumps(
        {
            "facts": [{"key": "rate", "value": "$100/hr", "category": "pricing"}],
            "summary": "[TIMESTAMP] User set hourly rate to $100.",
        }
    )
    mock_response = make_text_response(llm_response_text)

    messages: list[AgentMessage] = [
        UserMessage(content="My rate is $100 per hour."),
        AssistantMessage(content="Got it, saved your rate."),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved_facts, _ = await compact_session(test_user.id, messages, max_message_seq=2)

    assert len(saved_facts) == 1
    assert saved_facts[0]["key"] == "rate"

    memory_store = get_memory_store(test_user.id)
    assert memory_store._history_path.exists()
    history_content = memory_store._history_path.read_text(encoding="utf-8")
    assert "User set hourly rate to $100" in history_content
    # [TIMESTAMP] should have been replaced with an actual timestamp
    assert "[TIMESTAMP]" not in history_content
    assert "[20" in history_content


@pytest.mark.asyncio()
async def test_compact_session_no_summary_skips_history(test_user: UserData) -> None:
    """compact_session should not write HISTORY.md when summary is empty."""
    llm_response_text = json.dumps({"facts": [], "summary": ""})
    mock_response = make_text_response(llm_response_text)

    messages: list[AgentMessage] = [
        UserMessage(content="Hey"),
        AssistantMessage(content="Hi there!"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages, max_message_seq=2)

    memory_store = get_memory_store(test_user.id)
    assert not memory_store._history_path.exists()


@pytest.mark.asyncio()
async def test_compact_session_legacy_format_no_history(test_user: UserData) -> None:
    """Legacy array-format response should not write HISTORY.md."""
    llm_response_text = json.dumps([{"key": "fact", "value": "val", "category": "general"}])
    mock_response = make_text_response(llm_response_text)

    messages: list[AgentMessage] = [UserMessage(content="test")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages)

    memory_store = get_memory_store(test_user.id)
    assert not memory_store._history_path.exists()


# --- Session-end consolidation tests ---


@pytest.mark.asyncio()
async def test_consolidate_previous_session_triggers_compaction() -> None:
    """When a new session starts, unconsolidated messages from the previous
    session should trigger background compaction."""
    user_store = get_user_store()
    user = await user_store.create(
        user_id="consolidation-test",
        phone="+15550003333",
        channel_identifier="333",
        preferred_channel="telegram",
        onboarding_complete=True,
    )

    session_store = get_session_store(user.id)

    old_session, _ = await session_store.get_or_create_session()
    await session_store.add_message(old_session, MessageDirection.INBOUND, "My rate is $75/hr")
    await session_store.add_message(old_session, MessageDirection.OUTBOUND, "Got it, saved.")
    assert old_session.last_compacted_seq == 0

    new_session, is_new = await session_store.get_or_create_session(force_new=True)
    assert is_new
    assert new_session.session_id != old_session.session_id

    with patch(
        "backend.app.agent.context._run_compaction_in_background",
        new_callable=AsyncMock,
    ) as mock_compact:
        await _consolidate_previous_session(
            session_store,
            user.id,
            new_session.session_id,
        )

    mock_compact.assert_called_once()
    call_args = mock_compact.call_args
    agent_messages = call_args[0][3]
    assert len(agent_messages) == 2
    assert call_args[0][4] == 2


@pytest.mark.asyncio()
async def test_consolidate_previous_session_skips_already_compacted() -> None:
    """If the previous session was fully compacted, no compaction should trigger."""
    user_store = get_user_store()
    user = await user_store.create(
        user_id="consolidation-skip",
        phone="+15550004444",
        channel_identifier="444",
        preferred_channel="telegram",
        onboarding_complete=True,
    )

    session_store = get_session_store(user.id)

    old_session, _ = await session_store.get_or_create_session()
    await session_store.add_message(old_session, MessageDirection.INBOUND, "Hello")
    await session_store.add_message(old_session, MessageDirection.OUTBOUND, "Hi!")
    await session_store.update_compaction_seq(old_session, 2)

    new_session, is_new = await session_store.get_or_create_session(force_new=True)
    assert is_new

    with patch(
        "backend.app.agent.context._run_compaction_in_background",
        new_callable=AsyncMock,
    ) as mock_compact:
        await _consolidate_previous_session(
            session_store,
            user.id,
            new_session.session_id,
        )

    mock_compact.assert_not_called()


@pytest.mark.asyncio()
async def test_get_or_create_conversation_triggers_consolidation() -> None:
    """get_or_create_conversation should consolidate previous session on force_new."""
    user_store = get_user_store()
    user = await user_store.create(
        user_id="conv-consolidation",
        phone="+15550005555",
        channel_identifier="555",
        preferred_channel="telegram",
        onboarding_complete=True,
    )

    session_store = get_session_store(user.id)
    old_session, _ = await session_store.get_or_create_session()
    await session_store.add_message(old_session, MessageDirection.INBOUND, "Some info")

    with patch(
        "backend.app.agent.context._consolidate_previous_session",
        new_callable=AsyncMock,
    ) as mock_consolidate:
        _, is_new = await get_or_create_conversation(user.id, force_new=True)

    assert is_new
    mock_consolidate.assert_called_once()


@pytest.mark.asyncio()
async def test_get_or_create_conversation_no_consolidation_when_disabled() -> None:
    """get_or_create_conversation should skip consolidation when compaction disabled."""
    user_store = get_user_store()
    user = await user_store.create(
        user_id="conv-no-consolidation",
        phone="+15550006666",
        channel_identifier="666",
        preferred_channel="telegram",
        onboarding_complete=True,
    )

    session_store = get_session_store(user.id)
    old_session, _ = await session_store.get_or_create_session()
    await session_store.add_message(old_session, MessageDirection.INBOUND, "Some info")

    with (
        patch(
            "backend.app.agent.context._consolidate_previous_session",
            new_callable=AsyncMock,
        ) as mock_consolidate,
        patch("backend.app.agent.context.settings") as mock_settings,
    ):
        mock_settings.compaction_enabled = False
        _, is_new = await get_or_create_conversation(user.id, force_new=True)

    assert is_new
    mock_consolidate.assert_not_called()
