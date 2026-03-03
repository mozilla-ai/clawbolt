"""Tests for session compaction (extracting durable facts from aging messages)."""

import json
from unittest.mock import AsyncMock, patch

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

    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = llm_response_content

    messages = [
        UserMessage(content="I usually charge $45 per square foot for composite decks"),
        AssistantMessage(content="Got it, I'll remember that."),
        UserMessage(content="Oh and Mr. Smith's number is 555-0123"),
    ]

    with patch("backend.app.agent.compaction.acompletion", return_value=mock_response) as mock_llm:
        saved = await compact_session(db_session, test_contractor.id, messages)

    assert len(saved) == 2
    assert saved[0]["key"] == "deck_rate"
    assert saved[1]["key"] == "client_smith_phone"

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
    call_messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
    assert call_messages[0]["content"] == COMPACTION_SYSTEM_PROMPT


@pytest.mark.asyncio()
async def test_compact_session_empty_messages(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session with no messages should return empty list without LLM call."""
    with patch("backend.app.agent.compaction.acompletion") as mock_llm:
        saved = await compact_session(db_session, test_contractor.id, [])

    assert saved == []
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_disabled(db_session: Session, test_contractor: Contractor) -> None:
    """compact_session should skip when compaction_enabled is False."""
    messages = [UserMessage(content="Some content")]

    with (
        patch("backend.app.agent.compaction.settings") as mock_settings,
        patch("backend.app.agent.compaction.acompletion") as mock_llm,
    ):
        mock_settings.compaction_enabled = False
        saved = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []
    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_llm_failure_returns_empty(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should return empty list if LLM call fails."""
    messages = [UserMessage(content="Some content")]

    with patch(
        "backend.app.agent.compaction.acompletion",
        side_effect=Exception("LLM unavailable"),
    ):
        saved = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []


@pytest.mark.asyncio()
async def test_compact_session_invalid_llm_response(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should handle unparseable LLM responses gracefully."""
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "Sorry, I can't do that."

    messages = [UserMessage(content="Some content")]

    with patch("backend.app.agent.compaction.acompletion", return_value=mock_response):
        saved = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []


@pytest.mark.asyncio()
async def test_compact_session_no_durable_facts(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should handle LLM returning empty array (no facts)."""
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "[]"

    messages = [
        UserMessage(content="Hey there"),
        AssistantMessage(content="Hello! How can I help?"),
    ]

    with patch("backend.app.agent.compaction.acompletion", return_value=mock_response):
        saved = await compact_session(db_session, test_contractor.id, messages)

    assert saved == []
    memories = await get_all_memories(db_session, test_contractor.id)
    assert len(memories) == 0


@pytest.mark.asyncio()
async def test_compact_session_uses_configured_model(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should use compaction_model/provider when configured."""
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "[]"

    messages = [UserMessage(content="test")]

    with (
        patch("backend.app.agent.compaction.acompletion", return_value=mock_response) as mock_llm,
        patch("backend.app.agent.compaction.settings") as mock_settings,
    ):
        mock_settings.compaction_enabled = True
        mock_settings.compaction_model = "gpt-4o-mini"
        mock_settings.compaction_provider = "openai"
        mock_settings.compaction_max_tokens = 300
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        await compact_session(db_session, test_contractor.id, messages)

    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "gpt-4o-mini"


@pytest.mark.asyncio()
async def test_compact_session_falls_back_to_llm_model(
    db_session: Session, test_contractor: Contractor
) -> None:
    """compact_session should fall back to llm_model when compaction_model is empty."""
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "[]"

    messages = [UserMessage(content="test")]

    with (
        patch("backend.app.agent.compaction.acompletion", return_value=mock_response) as mock_llm,
        patch("backend.app.agent.compaction.settings") as mock_settings,
    ):
        mock_settings.compaction_enabled = True
        mock_settings.compaction_model = ""
        mock_settings.compaction_provider = ""
        mock_settings.compaction_max_tokens = 500
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        await compact_session(db_session, test_contractor.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "gpt-4o"
    assert call_kwargs.kwargs.get("provider") == "openai"


# --- Integration: load_conversation_history with compaction ---


@pytest.mark.asyncio()
async def test_load_history_triggers_compaction_when_full(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """When history exceeds limit, compaction should run on trimmed messages."""
    # Create 25 messages (limit is 20 by default, but we use limit=5 for the test)
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
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = llm_response_content

    with patch("backend.app.agent.compaction.acompletion", return_value=mock_response):
        # Use limit=5, so 3 messages are trimmed (8 total, 5 loaded, minus current = 4 history)
        history = await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )

    # History should have 4 messages (5 loaded minus 1 current)
    assert len(history) == 4

    # Compacted fact should have been saved
    memories = await get_all_memories(db_session, test_contractor.id)
    assert len(memories) == 1
    assert memories[0].key == "fact_from_compaction"


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

    with patch("backend.app.agent.compaction.acompletion") as mock_llm:
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

    with patch("backend.app.agent.compaction.acompletion") as mock_llm:
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
        "backend.app.agent.compaction.acompletion",
        side_effect=Exception("LLM down"),
    ):
        history = await load_conversation_history(
            db_session, conversation.id, limit=5, contractor_id=test_contractor.id
        )

    # History should still load correctly despite compaction failure
    assert len(history) == 4
