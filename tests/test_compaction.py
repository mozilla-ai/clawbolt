"""Tests for session compaction (consolidating aging messages into MEMORY.md)."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

import backend.app.database as _db_module
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
from backend.app.agent.file_store import SessionState, StoredMessage, UserData
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.session_db import get_session_store
from backend.app.enums import MessageDirection
from backend.app.models import User
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


def test_parse_valid_response() -> None:
    """Should parse a valid JSON object with memory_update and summary."""
    raw = json.dumps(
        {
            "memory_update": "## Pricing\n- Deck: $45/sqft",
            "summary": "[TIMESTAMP] Discussed pricing.",
        }
    )
    memory_update, summary = _parse_compaction_response(raw)
    assert "Deck: $45/sqft" in memory_update
    assert summary == "[TIMESTAMP] Discussed pricing."


def test_parse_empty_fields() -> None:
    """Should handle empty memory_update and summary."""
    raw = json.dumps({"memory_update": "", "summary": ""})
    memory_update, summary = _parse_compaction_response(raw)
    assert memory_update == ""
    assert summary == ""


def test_parse_markdown_fenced_json() -> None:
    """Should handle markdown code fences around JSON."""
    inner = json.dumps(
        {
            "memory_update": "## Facts\n- Rate: $50/hr",
            "summary": "A summary.",
        }
    )
    raw = f"```json\n{inner}\n```"
    memory_update, summary = _parse_compaction_response(raw)
    assert "Rate: $50/hr" in memory_update
    assert summary == "A summary."


def test_parse_prefilled_response() -> None:
    """Should parse a response missing the leading '{' from assistant prefill."""
    # The assistant prefill starts with "{", so the LLM response may omit it
    inner = json.dumps(
        {
            "memory_update": "## Notes\n- Prefers 8am starts",
            "summary": "[TIMESTAMP] Scheduling preferences.",
        }
    )
    # Strip the leading "{" to simulate what the LLM returns after prefill
    raw_without_brace = inner.lstrip("{")
    memory_update, summary = _parse_compaction_response(raw_without_brace)
    assert "8am starts" in memory_update
    assert summary == "[TIMESTAMP] Scheduling preferences."


def test_parse_invalid_json() -> None:
    """Invalid JSON should return empty strings without raising."""
    memory_update, summary = _parse_compaction_response("not json at all")
    assert memory_update == ""
    assert summary == ""


def test_parse_non_object_json() -> None:
    """Non-object JSON should return empty strings."""
    memory_update, summary = _parse_compaction_response("[1, 2, 3]")
    assert memory_update == ""
    assert summary == ""


# --- compact_session tests ---


@pytest.mark.asyncio()
async def test_compact_session_rewrites_memory(test_user: UserData) -> None:
    """compact_session should call LLM and write updated MEMORY.md."""
    llm_response_content = json.dumps(
        {
            "memory_update": "## Pricing\n- Deck: $45/sqft composite\n\n## Clients\n- Smith: 555-0123",
            "summary": "[TIMESTAMP] Discussed pricing and client info.",
        }
    )
    mock_response = make_text_response(llm_response_content)

    messages: list[AgentMessage] = [
        UserMessage(content="I usually charge $45 per square foot for composite decks"),
        AssistantMessage(content="Got it, I'll remember that."),
        UserMessage(content="Oh and Mr. Smith's number is 555-0123"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        memory_update, max_seq = await compact_session(test_user.id, messages)

    assert "Deck: $45/sqft" in memory_update
    assert "Smith: 555-0123" in memory_update
    assert max_seq is None

    # Verify MEMORY.md was written
    store = get_memory_store(test_user.id)
    content = store.read_memory()
    assert "Deck: $45/sqft" in content

    # Verify LLM was called with the system prompt and assistant prefill
    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("system") == COMPACTION_SYSTEM_PROMPT
    llm_messages = call_kwargs.kwargs["messages"]
    assert llm_messages[-1] == {"role": "assistant", "content": "{"}


@pytest.mark.asyncio()
async def test_compact_session_includes_current_memory_and_user(
    test_user: UserData,
) -> None:
    """compact_session should pass current MEMORY.md and USER.md to the LLM."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Existing\n- Old fact: still relevant")
    store.write_user("- Name: Nathan\n- Trade: General contractor")

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Existing\n- Old fact: still relevant", "summary": ""})
    )

    messages: list[AgentMessage] = [UserMessage(content="Just chatting")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await compact_session(test_user.id, messages)

    # Verify the LLM received current memory and user profile
    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]
    assert "Old fact: still relevant" in user_content
    assert "Nathan" in user_content
    assert "General contractor" in user_content


@pytest.mark.asyncio()
async def test_compact_session_returns_max_message_seq(test_user: UserData) -> None:
    """compact_session should return the max_message_seq when provided."""
    mock_response = make_text_response(
        json.dumps({"memory_update": "## Facts\n- fact: val", "summary": ""})
    )

    messages: list[AgentMessage] = [UserMessage(content="test")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        memory_update, max_seq = await compact_session(test_user.id, messages, max_message_seq=42)

    assert memory_update != ""
    assert max_seq == 42


@pytest.mark.asyncio()
async def test_compact_session_empty_messages(test_user: UserData) -> None:
    """compact_session with no messages should return empty without LLM call."""
    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        memory_update, max_seq = await compact_session(test_user.id, [])

    assert memory_update == ""
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
        memory_update, max_seq = await compact_session(test_user.id, messages)

    assert memory_update == ""
    assert max_seq is None
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_llm_failure_returns_empty(test_user: UserData) -> None:
    """compact_session should return empty string if LLM call fails."""
    messages: list[AgentMessage] = [UserMessage(content="Some content")]

    with patch(
        "backend.app.agent.compaction.amessages",
        side_effect=Exception("LLM unavailable"),
    ):
        memory_update, max_seq = await compact_session(test_user.id, messages)

    assert memory_update == ""
    assert max_seq is None


@pytest.mark.asyncio()
async def test_compact_session_invalid_llm_response(test_user: UserData) -> None:
    """compact_session should handle unparseable LLM responses gracefully."""
    mock_response = make_text_response("Sorry, I can't do that.")

    messages: list[AgentMessage] = [UserMessage(content="Some content")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        memory_update, max_seq = await compact_session(test_user.id, messages)

    assert memory_update == ""
    assert max_seq is None


@pytest.mark.asyncio()
async def test_compact_session_no_new_info(test_user: UserData) -> None:
    """compact_session should handle LLM returning empty memory_update."""
    mock_response = make_text_response(json.dumps({"memory_update": "", "summary": ""}))

    messages: list[AgentMessage] = [
        UserMessage(content="Hey there"),
        AssistantMessage(content="Hello! How can I help?"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        memory_update, max_seq = await compact_session(test_user.id, messages)

    assert memory_update == ""
    assert max_seq is None


@pytest.mark.asyncio()
async def test_compact_session_uses_configured_model(test_user: UserData) -> None:
    """compact_session should use compaction_model/provider when configured."""
    mock_response = make_text_response(json.dumps({"memory_update": "", "summary": ""}))

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
async def test_compact_session_falls_back_to_llm_model(test_user: UserData) -> None:
    """compact_session should fall back to llm_model when compaction_model is empty."""
    mock_response = make_text_response(json.dumps({"memory_update": "", "summary": ""}))

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
        {
            "memory_update": "## Extracted\n- fact_from_compaction: extracted",
            "summary": "",
        }
    )
    mock_response = make_text_response(llm_response_content)

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        history = await load_conversation_history(session, limit=5, user_id=test_user.id)
        await asyncio.sleep(0.1)

    assert len(history) == 4

    # Compacted memory should have been written
    store = get_memory_store(test_user.id)
    content = store.read_memory()
    assert "fact_from_compaction" in content


@pytest.mark.asyncio()
async def test_load_history_updates_last_compacted_seq(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Compaction should update last_compacted_seq on the session."""
    _add_messages(session, 8)

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Facts\n- f: v", "summary": ""})
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await load_conversation_history(session, limit=5, user_id=test_user.id)
        await asyncio.sleep(0.1)

    assert session.last_compacted_seq > 0


@pytest.mark.asyncio()
async def test_load_history_skips_already_compacted_messages(
    test_user: UserData,
    session: SessionState,
) -> None:
    """Messages already compacted should not be re-compacted."""
    _add_messages(session, 8)
    session.last_compacted_seq = 2

    mock_response = make_text_response(
        json.dumps({"memory_update": "## New\n- new_fact: from_remaining", "summary": ""})
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await load_conversation_history(session, limit=5, user_id=test_user.id)
        await asyncio.sleep(0.1)

    mock_llm.assert_called_once()
    call_messages = mock_llm.call_args.kwargs.get("messages") or mock_llm.call_args[1].get(
        "messages"
    )
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

    mock_llm.assert_not_called()
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
        await asyncio.sleep(0.1)

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
        user_id: str,
        trimmed_messages: list[object],
        max_message_seq: int | None = None,
    ) -> tuple[str, int | None]:
        compaction_started.set()
        await compaction_proceed.wait()
        return "", max_message_seq

    with patch("backend.app.agent.context.compact_session", side_effect=slow_compact):
        history = await load_conversation_history(session, limit=5, user_id=test_user.id)

        assert len(history) == 4

        await asyncio.sleep(0.05)
        assert compaction_started.is_set()

        compaction_proceed.set()
        await asyncio.sleep(0.05)


# --- compact_session HISTORY.md tests ---


@pytest.mark.asyncio()
async def test_compact_session_appends_history(test_user: UserData) -> None:
    """compact_session should write summary to HISTORY.md."""
    llm_response_text = json.dumps(
        {
            "memory_update": "## Pricing\n- Rate: $100/hr",
            "summary": "[TIMESTAMP] User set hourly rate to $100.",
        }
    )
    mock_response = make_text_response(llm_response_text)

    messages: list[AgentMessage] = [
        UserMessage(content="My rate is $100 per hour."),
        AssistantMessage(content="Got it, saved your rate."),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        memory_update, _ = await compact_session(test_user.id, messages, max_message_seq=2)

    assert "Rate: $100/hr" in memory_update

    memory_store = get_memory_store(test_user.id)
    history_content = memory_store.read_history()
    assert history_content  # non-empty
    assert "User set hourly rate to $100" in history_content
    assert "[TIMESTAMP]" not in history_content
    assert "[20" in history_content


@pytest.mark.asyncio()
async def test_compact_session_no_summary_skips_history(test_user: UserData) -> None:
    """compact_session should not write HISTORY.md when summary is empty."""
    llm_response_text = json.dumps({"memory_update": "", "summary": ""})
    mock_response = make_text_response(llm_response_text)

    messages: list[AgentMessage] = [
        UserMessage(content="Hey"),
        AssistantMessage(content="Hi there!"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages, max_message_seq=2)

    memory_store = get_memory_store(test_user.id)
    assert memory_store.read_history() == ""


# --- Session-end consolidation tests ---


@pytest.mark.asyncio()
async def test_consolidate_previous_session_triggers_compaction() -> None:
    """When a new session starts, unconsolidated messages from the previous
    session should trigger background compaction."""
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="consolidation-test",
            phone="+15550003333",
            channel_identifier="333",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

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
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="consolidation-skip",
            phone="+15550004444",
            channel_identifier="444",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

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
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="conv-consolidation",
            phone="+15550005555",
            channel_identifier="555",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

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
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="conv-no-consolidation",
            phone="+15550006666",
            channel_identifier="666",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

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
