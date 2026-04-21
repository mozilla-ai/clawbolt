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
from backend.app.agent.stores import HeartbeatStore
from backend.app.enums import MessageDirection
from backend.app.models import User
from tests.mocks.llm import extract_system_text, make_text_response


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
    result = _parse_compaction_response(raw)
    assert "Deck: $45/sqft" in result.memory_update
    assert result.summary == "[TIMESTAMP] Discussed pricing."


def test_parse_empty_fields() -> None:
    """Should handle empty memory_update and summary."""
    raw = json.dumps({"memory_update": "", "summary": ""})
    result = _parse_compaction_response(raw)
    assert result.memory_update == ""
    assert result.summary == ""


def test_parse_markdown_fenced_json() -> None:
    """Should handle markdown code fences around JSON."""
    inner = json.dumps(
        {
            "memory_update": "## Facts\n- Rate: $50/hr",
            "summary": "A summary.",
        }
    )
    raw = f"```json\n{inner}\n```"
    result = _parse_compaction_response(raw)
    assert "Rate: $50/hr" in result.memory_update
    assert result.summary == "A summary."


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
    result = _parse_compaction_response(raw_without_brace)
    assert "8am starts" in result.memory_update
    assert result.summary == "[TIMESTAMP] Scheduling preferences."


def test_parse_invalid_json() -> None:
    """Invalid JSON should return empty strings without raising."""
    result = _parse_compaction_response("not json at all")
    assert result.memory_update == ""
    assert result.summary == ""


def test_parse_non_object_json() -> None:
    """Non-object JSON should return empty strings."""
    result = _parse_compaction_response("[1, 2, 3]")
    assert result.memory_update == ""
    assert result.summary == ""


def test_parse_user_profile_update() -> None:
    """Should parse user_profile_update field from compaction response."""
    raw = json.dumps(
        {
            "memory_update": "",
            "summary": "",
            "user_profile_update": "- Name: Nathan\n- Day rate: $500",
            "soul_update": "",
        }
    )
    result = _parse_compaction_response(raw)
    assert result.memory_update == ""
    assert "Day rate: $500" in result.user_profile_update
    assert result.soul_update == ""


def test_parse_soul_update() -> None:
    """Should parse soul_update field from compaction response."""
    raw = json.dumps(
        {
            "memory_update": "",
            "summary": "",
            "user_profile_update": "",
            "soul_update": "Be more concise. Skip the pleasantries.",
        }
    )
    result = _parse_compaction_response(raw)
    assert "Be more concise" in result.soul_update


def test_parse_missing_new_fields_defaults_empty() -> None:
    """Responses without user_profile_update/soul_update should default to empty."""
    raw = json.dumps(
        {
            "memory_update": "## Facts\n- something",
            "summary": "A summary.",
        }
    )
    result = _parse_compaction_response(raw)
    assert result.user_profile_update == ""
    assert result.soul_update == ""


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

    # Verify LLM was called with the system prompt
    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert extract_system_text(call_kwargs.kwargs.get("system")) == COMPACTION_SYSTEM_PROMPT
    llm_messages = call_kwargs.kwargs["messages"]
    assert llm_messages[-1]["role"] == "user"


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

    # Verify the LLM received current memory and user profile in XML-tagged sections
    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]
    assert "Old fact: still relevant" in user_content
    assert "Nathan" in user_content
    assert "General contractor" in user_content
    # Verify XML tags are used to separate sections (prevents user profile leaking)
    assert "<current_memory>" in user_content
    assert "</current_memory>" in user_content
    assert "<user_profile>" in user_content
    assert "</user_profile>" in user_content
    assert "<conversation>" in user_content
    assert "</conversation>" in user_content


@pytest.mark.asyncio()
async def test_compact_session_user_profile_in_separate_xml_section(
    test_user: UserData,
) -> None:
    """Regression test for #823: user profile must be in a distinct <user_profile>
    section so the LLM does not merge it into memory_update."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Clients\n- Bob: 555-0100")
    store.write_user("- Name: Nathan\n- Trade: General contractor\n- Location: Portland")

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Clients\n- Bob: 555-0100", "summary": ""})
    )

    messages: list[AgentMessage] = [UserMessage(content="Just chatting")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]

    # Memory content must be inside <current_memory> tags
    mem_start = user_content.index("<current_memory>")
    mem_end = user_content.index("</current_memory>")
    memory_section = user_content[mem_start:mem_end]
    assert "Bob: 555-0100" in memory_section

    # User profile must be inside <user_profile> tags, NOT in <current_memory>
    prof_start = user_content.index("<user_profile>")
    prof_end = user_content.index("</user_profile>")
    profile_section = user_content[prof_start:prof_end]
    assert "Nathan" in profile_section
    assert "General contractor" in profile_section
    assert "Portland" in profile_section

    # User profile content must NOT appear in the memory section
    assert "Nathan" not in memory_section
    assert "General contractor" not in memory_section
    assert "Portland" not in memory_section

    # System prompt should reference XML structure
    system_prompt = extract_system_text(call_kwargs.kwargs.get("system"))
    assert "<user_profile>" in system_prompt
    assert "<current_memory>" in system_prompt


@pytest.mark.asyncio()
async def test_compact_session_includes_soul_and_heartbeat(
    test_user: UserData,
) -> None:
    """compact_session should pass soul and heartbeat text to the LLM in XML tags."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Clients\n- Alice: 555-0100")
    store.write_soul("You are a friendly assistant for trades professionals.")

    heartbeat_store = HeartbeatStore(test_user.id)
    await heartbeat_store.write_heartbeat_md("- Follow up with Bob about the deck estimate")

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Clients\n- Alice: 555-0100", "summary": ""})
    )

    messages: list[AgentMessage] = [UserMessage(content="Just chatting")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]

    assert "<soul>" in user_content
    assert "</soul>" in user_content
    assert "friendly assistant for trades professionals" in user_content

    assert "<heartbeat>" in user_content
    assert "</heartbeat>" in user_content
    assert "Follow up with Bob about the deck estimate" in user_content


@pytest.mark.asyncio()
async def test_compact_session_soul_in_separate_xml_section(
    test_user: UserData,
) -> None:
    """Regression: soul content must be in <soul>, not in <current_memory>."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Clients\n- Bob: 555-0100")
    store.write_soul("You are a helpful construction assistant.")

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Clients\n- Bob: 555-0100", "summary": ""})
    )

    messages: list[AgentMessage] = [UserMessage(content="Just chatting")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]

    # Soul content must be inside <soul> tags
    soul_start = user_content.index("<soul>")
    soul_end = user_content.index("</soul>")
    soul_section = user_content[soul_start:soul_end]
    assert "helpful construction assistant" in soul_section

    # Soul content must NOT appear in the memory section
    mem_start = user_content.index("<current_memory>")
    mem_end = user_content.index("</current_memory>")
    memory_section = user_content[mem_start:mem_end]
    assert "helpful construction assistant" not in memory_section


@pytest.mark.asyncio()
async def test_compact_session_heartbeat_in_separate_xml_section(
    test_user: UserData,
) -> None:
    """Regression: heartbeat content must be in <heartbeat>, not in <current_memory>."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Facts\n- Rate: $50/hr")

    heartbeat_store = HeartbeatStore(test_user.id)
    await heartbeat_store.write_heartbeat_md("- Call supplier about lumber delivery")

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Facts\n- Rate: $50/hr", "summary": ""})
    )

    messages: list[AgentMessage] = [UserMessage(content="Just chatting")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]

    # Heartbeat content must be inside <heartbeat> tags
    hb_start = user_content.index("<heartbeat>")
    hb_end = user_content.index("</heartbeat>")
    heartbeat_section = user_content[hb_start:hb_end]
    assert "Call supplier about lumber delivery" in heartbeat_section

    # Heartbeat content must NOT appear in the memory section
    mem_start = user_content.index("<current_memory>")
    mem_end = user_content.index("</current_memory>")
    memory_section = user_content[mem_start:mem_end]
    assert "Call supplier about lumber delivery" not in memory_section


@pytest.mark.asyncio()
async def test_compact_session_empty_soul_and_heartbeat(
    test_user: UserData,
) -> None:
    """When soul and heartbeat are unset, their XML sections should show '(empty)'."""
    mock_response = make_text_response(json.dumps({"memory_update": "", "summary": ""}))

    messages: list[AgentMessage] = [UserMessage(content="Just chatting")]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response) as mock_llm:
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs is not None
    user_content = call_kwargs.kwargs["messages"][0]["content"]

    # Soul section should have (empty) placeholder
    soul_start = user_content.index("<soul>")
    soul_end = user_content.index("</soul>")
    soul_section = user_content[soul_start:soul_end]
    assert "(empty)" in soul_section

    # Heartbeat section should have (empty) placeholder
    hb_start = user_content.index("<heartbeat>")
    hb_end = user_content.index("</heartbeat>")
    heartbeat_section = user_content[hb_start:hb_end]
    assert "(empty)" in heartbeat_section


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
async def test_compact_session_writes_user_profile(test_user: UserData) -> None:
    """compact_session should write USER.md when LLM returns user_profile_update."""
    store = get_memory_store(test_user.id)
    store.write_user("- Name: Nathan\n- Trade: General contractor")

    llm_response_content = json.dumps(
        {
            "memory_update": "",
            "summary": "[TIMESTAMP] User shared their day rate.",
            "user_profile_update": "- Name: Nathan\n- Trade: General contractor\n- Day rate: $500",
            "soul_update": "",
        }
    )
    mock_response = make_text_response(llm_response_content)

    messages: list[AgentMessage] = [
        UserMessage(content="My day rate is $500"),
        AssistantMessage(content="Got it, updated the estimate."),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages)

    updated_profile = store.read_user()
    assert "Day rate: $500" in updated_profile
    assert "Nathan" in updated_profile
    assert "General contractor" in updated_profile


@pytest.mark.asyncio()
async def test_compact_session_writes_soul(test_user: UserData) -> None:
    """compact_session should write SOUL.md when LLM returns soul_update."""
    store = get_memory_store(test_user.id)
    store.write_soul("You are a friendly assistant.")

    llm_response_content = json.dumps(
        {
            "memory_update": "",
            "summary": "[TIMESTAMP] User asked for more direct communication.",
            "user_profile_update": "",
            "soul_update": "You are a direct, no-nonsense assistant. Skip the pleasantries.",
        }
    )
    mock_response = make_text_response(llm_response_content)

    messages: list[AgentMessage] = [
        UserMessage(content="Be more blunt with me, skip the niceties"),
        AssistantMessage(content="Done. I'll keep it direct from now on."),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages)

    updated_soul = store.read_soul()
    assert "direct, no-nonsense" in updated_soul
    assert "Skip the pleasantries" in updated_soul


@pytest.mark.asyncio()
async def test_compact_session_skips_empty_profile_and_soul(test_user: UserData) -> None:
    """compact_session should not write USER.md or SOUL.md when updates are empty."""
    store = get_memory_store(test_user.id)
    store.write_user("- Name: Nathan")
    store.write_soul("You are helpful.")

    llm_response_content = json.dumps(
        {
            "memory_update": "## Clients\n- Bob: 555-0100",
            "summary": "[TIMESTAMP] Discussed client info.",
            "user_profile_update": "",
            "soul_update": "",
        }
    )
    mock_response = make_text_response(llm_response_content)

    messages: list[AgentMessage] = [
        UserMessage(content="Bob's number is 555-0100"),
        AssistantMessage(content="Saved Bob's number."),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages)

    # Profile and soul should be unchanged
    assert store.read_user() == "- Name: Nathan"
    assert store.read_soul() == "You are helpful."
    # Memory should be updated
    assert "Bob: 555-0100" in store.read_memory()


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


# --- Integration: load_conversation_history no longer triggers compaction ---
# Compaction is now triggered from process_message() when trim_messages() drops
# messages, not from load_conversation_history(). The tests below verify the new
# behavior. See test_agent.py for trim-triggered compaction tests.


@pytest.mark.asyncio()
async def test_load_history_returns_all_messages_under_limit(
    test_user: UserData,
    session: SessionState,
) -> None:
    """load_conversation_history should return all messages when under the soft limit."""
    _add_messages(session, 8)

    # No compaction should be triggered from load_history anymore
    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        history = await load_conversation_history(session, limit=500)

    mock_llm.assert_not_called()
    # 8 messages, minus 1 for current = 7
    assert len(history) == 7


@pytest.mark.asyncio()
async def test_load_history_soft_limit_caps_messages(
    test_user: UserData,
    session: SessionState,
) -> None:
    """The soft limit should still cap how many messages are loaded into memory."""
    _add_messages(session, 10)

    history = await load_conversation_history(session, limit=5)
    # 5 loaded, minus 1 for current = 4
    assert len(history) == 4


@pytest.mark.asyncio()
async def test_load_history_no_compaction_regardless_of_count(
    test_user: UserData,
    session: SessionState,
) -> None:
    """load_conversation_history should never trigger compaction, even over limit."""
    _add_messages(session, 100)

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        history = await load_conversation_history(session, limit=5)

    # No compaction LLM call should happen from load_history
    mock_llm.assert_not_called()
    assert len(history) == 4


# --- trigger_compaction_for_dropped tests ---


@pytest.mark.asyncio()
async def test_trigger_compaction_for_dropped_fires_background_task(
    test_user: UserData,
) -> None:
    """trigger_compaction_for_dropped should fire a background compaction task."""
    from backend.app.agent.context import trigger_compaction_for_dropped

    dropped: list[AgentMessage] = [
        UserMessage(content="Old message 1"),
        AssistantMessage(content="Old reply 1"),
    ]

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Facts\n- fact: from_trim", "summary": ""})
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        trigger_compaction_for_dropped(test_user.id, dropped)
        await asyncio.sleep(0.2)

    store = get_memory_store(test_user.id)
    content = store.read_memory()
    assert "fact: from_trim" in content


@pytest.mark.asyncio()
async def test_trigger_compaction_for_dropped_skips_empty(
    test_user: UserData,
) -> None:
    """trigger_compaction_for_dropped should do nothing with empty dropped list."""
    from backend.app.agent.context import trigger_compaction_for_dropped

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        trigger_compaction_for_dropped(test_user.id, [])
        await asyncio.sleep(0.1)

    mock_llm.assert_not_called()


@pytest.mark.asyncio()
async def test_trigger_compaction_for_dropped_skips_when_disabled(
    test_user: UserData,
) -> None:
    """trigger_compaction_for_dropped should skip when compaction is disabled."""
    from backend.app.agent.context import trigger_compaction_for_dropped

    dropped: list[AgentMessage] = [UserMessage(content="Old message")]

    with (
        patch("backend.app.agent.context.settings") as mock_settings,
        patch("backend.app.agent.compaction.amessages") as mock_llm,
    ):
        mock_settings.compaction_enabled = False
        trigger_compaction_for_dropped(test_user.id, dropped)
        await asyncio.sleep(0.1)

    mock_llm.assert_not_called()


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


@pytest.mark.asyncio()
async def test_run_compaction_skips_when_watermark_already_advanced() -> None:
    """If last_compacted_seq is already >= max_message_seq (e.g. another worker
    already compacted these messages, or a previous run crashed between
    compact_session and update_compaction_seq), we must not re-run compaction.

    Without this guard, the LLM rewrites MEMORY.md again and, worse, appends
    a duplicate entry to HISTORY.md every time.
    """
    from backend.app.agent.context import _run_compaction_in_background

    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="compaction-watermark-race",
            phone="+15550007777",
            channel_identifier="777",
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
    session, _ = await session_store.get_or_create_session()
    await session_store.add_message(session, MessageDirection.INBOUND, "msg 1")
    await session_store.add_message(session, MessageDirection.OUTBOUND, "msg 2")
    await session_store.update_compaction_seq(session, 5)

    stale_session_view = session_store.load_session(session.session_id)
    assert stale_session_view is not None
    stale_session_view.last_compacted_seq = 0

    messages: list[AgentMessage] = [UserMessage(content="msg 1"), AssistantMessage(content="msg 2")]

    with patch("backend.app.agent.context.compact_session", new_callable=AsyncMock) as mock_compact:
        await _run_compaction_in_background(
            session_store, stale_session_view, user.id, messages, max_message_seq=3
        )

    mock_compact.assert_not_called()


@pytest.mark.asyncio()
async def test_run_compaction_skips_when_peer_worker_holds_lock() -> None:
    """When another worker already holds the per-session compaction advisory
    lock, this worker must return without running compact_session so both
    workers can't race and append duplicate HISTORY.md entries.

    The watermark re-check alone doesn't prevent the race: two workers can
    both read the old watermark, both call the LLM, and both append before
    either one writes the new seq. The advisory lock closes that window.
    """
    import backend.app.agent.context as context_module
    from backend.app.agent.context import _run_compaction_in_background

    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="compaction-lock-race",
            phone="+15550009999",
            channel_identifier="999",
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
    session, _ = await session_store.get_or_create_session()

    messages: list[AgentMessage] = [UserMessage(content="a"), AssistantMessage(content="b")]

    class _LockedSession:
        """SessionLocal stand-in where pg_try_advisory_lock returns False."""

        def execute(self, *_args: object, **_kwargs: object) -> object:
            class _Result:
                def scalar(self) -> bool:
                    return False

            return _Result()

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    with (
        patch.object(context_module, "SessionLocal", _LockedSession),
        patch("backend.app.agent.context.compact_session", new_callable=AsyncMock) as mock_compact,
    ):
        await _run_compaction_in_background(
            session_store, session, user.id, messages, max_message_seq=10
        )

    mock_compact.assert_not_called()


@pytest.mark.asyncio()
async def test_concurrent_get_or_create_session_does_not_duplicate() -> None:
    """Two concurrent get_or_create_session calls for the same user must not
    create two active sessions.

    Regression: there is no uniqueness constraint on (user_id, is_active=True),
    so without serialization both callers see 'no session' and both insert.
    The advisory lock in get_or_create_session should serialize them so the
    second caller sees the first's committed row.
    """
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="concurrent-session-race",
            phone="+15550008888",
            channel_identifier="888",
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

    results = await asyncio.gather(
        session_store.get_or_create_session(),
        session_store.get_or_create_session(),
    )
    session_a, is_new_a = results[0]
    session_b, is_new_b = results[1]

    assert session_a.session_id == session_b.session_id, (
        "concurrent callers should converge on the same session"
    )
    assert is_new_a != is_new_b, (
        "exactly one caller should have created the session; the other reused it"
    )

    db = _db_module.SessionLocal()
    try:
        from backend.app.models import ChatSession as CS

        active_count = db.query(CS).filter_by(user_id=user.id, is_active=True).count()
    finally:
        db.close()

    assert active_count == 1, f"expected 1 active session, got {active_count}"
