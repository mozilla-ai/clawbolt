"""Tests for session compaction (extracting durable facts from aging messages)."""

import asyncio
import json
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    _format_messages_for_compaction,
    _parse_compaction_response,
    compact_session,
)
from backend.app.agent.context import load_conversation_history
from backend.app.agent.memory import get_all_memories
from backend.app.agent.messages import AssistantMessage, UserMessage
from backend.app.models import Contractor, Conversation, Message
from tests.mocks.llm import make_text_response


@pytest.fixture()
def conversation(db_session: Session, test_contractor: Contractor) -> Conversation:
    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    return conv


# --- _format_messages_for_compaction tests ---


def test_format_messages_basic() -> None:
    """Format should produce readable Contractor/Assistant lines."""
    messages = [
        UserMessage(content="I charge $45/sqft for composite decks"),
        AssistantMessage(content="Got it, I'll remember that pricing."),
        UserMessage(content="My supplier is ABC Lumber on 5th Ave"),
    ]
    result = _format_messages_for_compaction(messages)
    assert "Contractor: I charge $45/sqft" in result
    assert "Assistant: Got it" in result
    assert "Contractor: My supplier is ABC Lumber" in result


def test_format_messages_empty() -> None:
    """Empty message list should produce empty string."""
    assert _format_messages_for_compaction([]) == ""


def test_format_messages_skips_empty_assistant() -> None:
    """Assistant messages with no content should be skipped."""
    messages = [
        UserMessage(content="Hello"),
        AssistantMessage(content=None),
        UserMessage(content="World"),
    ]
    result = _format_messages_for_compaction(messages)
    assert "Assistant:" not in result
    assert "Contractor: Hello" in result
    assert "Contractor: World" in result


# --- _parse_compaction_response tests ---


def test_parse_valid_json_array() -> None:
    """Should parse a valid JSON array of fact objects."""
    raw = json.dumps(
        [
            {"key": "deck_pricing", "value": "$45/sqft composite", "category": "pricing"},
            {"key": "supplier_name", "value": "ABC Lumber", "category": "supplier"},
        ]
    )
    facts = _parse_compaction_response(raw)
    assert len(facts) == 2
    assert facts[0]["key"] == "deck_pricing"
    assert facts[0]["value"] == "$45/sqft composite"
    assert facts[0]["category"] == "pricing"
    assert facts[1]["key"] == "supplier_name"


def test_parse_markdown_fenced_json() -> None:
    """Should handle markdown code fences around JSON."""
    raw = '```json\n[{"key": "rate", "value": "$50/hr", "category": "pricing"}]\n```'
    facts = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "rate"


def test_parse_empty_array() -> None:
    """Empty JSON array should return empty list."""
    facts = _parse_compaction_response("[]")
    assert facts == []


def test_parse_invalid_json() -> None:
    """Invalid JSON should return empty list without raising."""
    facts = _parse_compaction_response("not json at all")
    assert facts == []


def test_parse_non_array_json() -> None:
    """Non-array JSON should return empty list."""
    facts = _parse_compaction_response('{"key": "val"}')
    assert facts == []


def test_parse_skips_items_without_key() -> None:
    """Items missing 'key' should be skipped."""
    raw = json.dumps(
        [
            {"value": "no key here", "category": "general"},
            {"key": "good_fact", "value": "this is valid", "category": "general"},
        ]
    )
    facts = _parse_compaction_response(raw)
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
    facts = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "good"


def test_parse_normalizes_invalid_category() -> None:
    """Unknown categories should be normalized to 'general'."""
    raw = json.dumps(
        [
            {"key": "fact1", "value": "something", "category": "unknown_cat"},
        ]
    )
    facts = _parse_compaction_response(raw)
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
    facts = _parse_compaction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "valid"


# --- compact_session tests ---


@pytest.mark.asyncio()
async def test_compact_session_extracts_and_saves_facts(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should call LLM and save extracted facts to memory."""
    llm_response_content = json.dumps(
        [
            {"key": "deck_rate", "value": "$45/sqft for composite", "category": "pricing"},
            {"key": "client_smith_phone", "value": "555-0123", "category": "client"},
        ]
    )

    mock_response = make_text_response(llm_response_content)

    messages = [
        UserMessage(content="I usually charge $45 per square foot for composite decks"),
        AssistantMessage(content="Got it, I'll remember that."),
        UserMessage(content="Oh and Mr. Smith's number is 555-0123"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        saved, max_id = await compact_session(db_session, test_contractor.id, messages)

    assert len(saved) == 2
    assert saved[0]["key"] == "deck_rate"
    assert saved[1]["key"] == "client_smith_phone"
    # No max_message_id passed, so should be None
    assert max_id is None

    # Verify facts were persisted in the database
    memories = await get_all_memories(db_session, test_contractor.id)
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
async def test_compact_session_returns_max_message_id(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should return the max_message_id when provided."""
    mock_response = make_text_response(
        json.dumps([{"key": "fact", "value": "val", "category": "general"}])
    )

    messages = [UserMessage(content="test")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved, max_id = await compact_session(
            db_session, test_contractor.id, messages, max_message_id=42
        )

    assert len(saved) == 1
    assert max_id == 42


@pytest.mark.asyncio()
async def test_compact_session_empty_messages(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session with no messages should return empty list without LLM call."""
    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        saved, max_id = await compact_session(db_session, test_contractor.id, [])

    assert saved == []
    assert max_id is None
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_disabled(db_session: Session, test_contractor: Contractor) -> None:
    """compact_session should skip when compaction_enabled is False."""
    messages = [UserMessage(content="Some content")]

    with (
        patch("backend.app.agent.compaction.settings") as mock_settings,
        patch("backend.app.agent.compaction.amessages") as mock_llm,
    ):
        mock_settings.compaction_enabled = False
        saved, max_id = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []
    assert max_id is None
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_llm_failure_returns_empty(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should return empty list if LLM call fails."""
    messages = [UserMessage(content="Some content")]

    with patch(
        "backend.app.agent.compaction.amessages",
        side_effect=Exception("LLM unavailable"),
    ):
        saved, max_id = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []
    assert max_id is None


@pytest.mark.asyncio()
async def test_compact_session_invalid_llm_response(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should handle unparseable LLM responses gracefully."""
    mock_response = make_text_response("Sorry, I can't do that.")

    messages = [UserMessage(content="Some content")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved, max_id = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []
    assert max_id is None


@pytest.mark.asyncio()
async def test_compact_session_no_durable_facts(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should handle LLM returning empty array (no facts)."""
    mock_response = make_text_response("[]")

    messages = [
        UserMessage(content="Hey there"),
        AssistantMessage(content="Hello! How can I help?"),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        saved, max_id = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []
    assert max_id is None
    memories = await get_all_memories(db_session, test_contractor.id)
    assert len(memories) == 0


@pytest.mark.asyncio()
async def test_compact_session_uses_configured_model(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should use compaction_model/provider when configured."""
    mock_response = make_text_response("[]")

    messages = [UserMessage(content="test")]

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
        await compact_session(db_session, test_contractor.id, messages)

    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "test-compact-model"


@pytest.mark.asyncio()
async def test_compact_session_falls_back_to_llm_model(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should fall back to llm_model when compaction_model is empty."""
    mock_response = make_text_response("[]")

    messages = [UserMessage(content="test")]

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
        await compact_session(db_session, test_contractor.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "test-model"
    assert call_kwargs.kwargs.get("provider") == "test-provider"


# --- Integration: load_conversation_history with compaction ---


@pytest.mark.asyncio()
async def test_load_history_triggers_compaction_when_full(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """When history exceeds limit, compaction should run on trimmed messages."""
    # Create 8 messages (we will use limit=5 for the test)
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound" if i % 2 == 0 else "outbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    llm_response_content = json.dumps(
        [
            {"key": "fact_from_compaction", "value": "extracted", "category": "general"},
        ]
    )
    mock_response = make_text_response(llm_response_content)

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        # Use limit=5, so 3 messages are trimmed (8 total, 5 loaded, minus current = 4 history)
        history = await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )
        # Allow the background compaction task to complete
        await asyncio.sleep(0.1)

    # History should have 4 messages (5 loaded minus 1 current)
    assert len(history) == 4

    # Compacted fact should have been saved
    memories = await get_all_memories(db_session, test_contractor.id)
    assert len(memories) == 1
    assert memories[0].key == "fact_from_compaction"


@pytest.mark.asyncio()
async def test_load_history_updates_last_compacted_message_id(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """Compaction should update last_compacted_message_id on the conversation."""
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    mock_response = make_text_response(
        json.dumps([{"key": "f", "value": "v", "category": "general"}])
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )
        await asyncio.sleep(0.1)

    db_session.refresh(conversation)
    assert conversation.last_compacted_message_id is not None
    # The trimmed messages are the first 3 (8 total - 5 limit), so the max ID
    # should be the 3rd message's ID.
    all_msgs = (
        db_session.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.id.asc())
        .all()
    )
    assert conversation.last_compacted_message_id == all_msgs[2].id


@pytest.mark.asyncio()
async def test_load_history_skips_already_compacted_messages(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """Messages already compacted should not be re-compacted."""
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    # Get all message IDs
    all_msgs = (
        db_session.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.id.asc())
        .all()
    )

    # Simulate that the first 2 messages were already compacted
    conversation.last_compacted_message_id = all_msgs[1].id
    db_session.commit()

    mock_response = make_text_response(
        json.dumps([{"key": "new_fact", "value": "from_remaining", "category": "general"}])
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )
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
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """When all trimmed messages are already compacted, no LLM call should happen."""
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    all_msgs = (
        db_session.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.id.asc())
        .all()
    )

    # Mark all trimmed messages as already compacted (first 3 are trimmed with limit=5)
    conversation.last_compacted_message_id = all_msgs[2].id
    db_session.commit()

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )
        await asyncio.sleep(0.1)

    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_load_history_no_compaction_when_under_limit(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """When history is under limit, no compaction should occur."""
    for i in range(3):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        history = await load_conversation_history(
            db_session, conversation.id, limit=20, contractor_id=test_contractor.id
        )

    # Should not call LLM since we're under the limit
    mock_llm.assert_not_called()
    # 3 messages, minus current = 2
    assert len(history) == 2


@pytest.mark.asyncio()
async def test_load_history_no_compaction_without_contractor_id(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """Without contractor_id, compaction should not run even at limit."""
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        history = await load_conversation_history(db_session, conversation.id, limit=5)

    mock_llm.assert_not_called()
    assert len(history) == 4


@pytest.mark.asyncio()
async def test_load_history_compaction_failure_does_not_break_history(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """Compaction failure should not prevent history from loading."""
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    with patch(
        "backend.app.agent.compaction.amessages",
        side_effect=Exception("LLM down"),
    ):
        history = await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )
        # Allow the background task to complete (and fail gracefully)
        await asyncio.sleep(0.1)

    # History should still load correctly despite compaction failure
    assert len(history) == 4


@pytest.mark.asyncio()
async def test_compaction_runs_in_background_not_blocking(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """Compaction should run as a background task, not blocking history loading."""
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                direction="inbound",
                body=f"Message {i}",
            )
        )
    db_session.commit()

    compaction_started = asyncio.Event()
    compaction_proceed = asyncio.Event()

    async def slow_compact(
        db: Session,
        contractor_id: int,
        trimmed_messages: list[object],
        max_message_id: int | None = None,
    ) -> tuple[list[dict[str, str]], int | None]:
        compaction_started.set()
        await compaction_proceed.wait()
        return [], max_message_id

    with patch("backend.app.agent.context.compact_session", side_effect=slow_compact):
        history = await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )

        # History should be returned immediately, even though compaction hasn't finished
        assert len(history) == 4

        # Compaction task should have started
        await asyncio.sleep(0.05)
        assert compaction_started.is_set()

        # Let compaction finish
        compaction_proceed.set()
        await asyncio.sleep(0.05)
