"""Tests for session compaction (consolidating aging messages into MEMORY.md)."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.agent.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    _build_snapshot_pairs,
    _format_messages_for_compaction,
    _parse_compaction_response,
    _serialize_snapshot,
    compact_session,
)
from backend.app.agent.context import (
    load_conversation_history,
    trigger_compaction_for_dropped,
)
from backend.app.agent.file_store import SessionState, StoredMessage, UserData
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.session_db import get_session_store
from backend.app.agent.stores import HeartbeatStore
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, CompactionEvent, User
from tests.db_test_utils import open_test_db_session
from tests.mocks.llm import extract_system_text, make_text_response


@pytest.fixture()
def session(test_user: UserData) -> SessionState:
    """Create a SessionState for the test user."""
    return SessionState(
        session_id="test-session",
        user_id=test_user.id,
        messages=[],
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
    content = await store.read_memory_async()
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
    await store.write_memory_async("## Existing\n- Old fact: still relevant")
    await store.write_user_async("- Name: Nathan\n- Trade: General contractor")

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
    await store.write_memory_async("## Clients\n- Bob: 555-0100")
    await store.write_user_async(
        "- Name: Nathan\n- Trade: General contractor\n- Location: Portland"
    )

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
    await store.write_memory_async("## Clients\n- Alice: 555-0100")
    await store.write_soul_async("You are a friendly assistant for trades professionals.")

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
    await store.write_memory_async("## Clients\n- Bob: 555-0100")
    await store.write_soul_async("You are a helpful construction assistant.")

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
    await store.write_memory_async("## Facts\n- Rate: $50/hr")

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
    await store.write_user_async("- Name: Nathan\n- Trade: General contractor")

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

    updated_profile = await store.read_user_async()
    assert "Day rate: $500" in updated_profile
    assert "Nathan" in updated_profile
    assert "General contractor" in updated_profile


@pytest.mark.asyncio()
async def test_compact_session_writes_soul(test_user: UserData) -> None:
    """compact_session should write SOUL.md when LLM returns soul_update."""
    store = get_memory_store(test_user.id)
    await store.write_soul_async("You are a friendly assistant.")

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

    updated_soul = await store.read_soul_async()
    assert "direct, no-nonsense" in updated_soul
    assert "Skip the pleasantries" in updated_soul


@pytest.mark.asyncio()
async def test_compact_session_skips_empty_profile_and_soul(test_user: UserData) -> None:
    """compact_session should not write USER.md or SOUL.md when updates are empty."""
    store = get_memory_store(test_user.id)
    await store.write_user_async("- Name: Nathan")
    await store.write_soul_async("You are helpful.")

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
    assert await store.read_user_async() == "- Name: Nathan"
    assert await store.read_soul_async() == "You are helpful."
    # Memory should be updated
    assert "Bob: 555-0100" in await store.read_memory_async()


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
async def test_compact_session_emits_structured_summary_log(
    test_user: UserData, caplog: pytest.LogCaptureFixture
) -> None:
    """compact_session must emit a single ``compaction.summary`` line with the
    fields downstream telemetry will aggregate.

    Ties down the field set so changes here are intentional. Compaction is
    a routine operation for active users (every ~27 days at 15k tokens/day),
    so the summary line is the primary signal we have for tracking
    frequency, cost, and whether the LLM is producing meaningful updates.
    """
    import logging

    llm_response_content = json.dumps(
        {
            "memory_update": "## Pricing\n- Day rate: $500",
            "summary": "[TIMESTAMP] Discussed pricing.",
            "user_profile_update": "- Day rate: $500",
            "soul_update": "",
        }
    )
    mock_response = make_text_response(
        llm_response_content,
        input_tokens=1234,
        output_tokens=42,
    )

    messages: list[AgentMessage] = [
        UserMessage(content="My day rate is $500"),
        AssistantMessage(content="Got it."),
    ]

    with (
        caplog.at_level(logging.INFO, logger="backend.app.agent.compaction"),
        patch("backend.app.agent.compaction.amessages", return_value=mock_response),
    ):
        await compact_session(test_user.id, messages)

    summary_lines = [r for r in caplog.records if "compaction.summary" in r.getMessage()]
    assert len(summary_lines) == 1, (
        f"expected exactly one compaction.summary line, got {len(summary_lines)}"
    )
    msg = summary_lines[0].getMessage()
    assert f"user={test_user.id}" in msg
    assert "trimmed_count=2" in msg
    assert "input_tokens=1234" in msg
    assert "output_tokens=42" in msg
    assert "memory_updated=True" in msg
    assert "user_updated=True" in msg
    assert "soul_updated=False" in msg
    # summary_len is the byte length of the parsed summary field, which
    # has the [TIMESTAMP] placeholder still in it (the run replaces it
    # before appending to HISTORY.md, but the log captures the parsed
    # value, not the substituted one).
    assert "summary_len=" in msg
    assert "duration_ms=" in msg


@pytest.mark.asyncio()
async def test_compact_session_summary_log_marks_all_updates_false_when_llm_returns_empty(
    test_user: UserData, caplog: pytest.LogCaptureFixture
) -> None:
    """When the LLM returns empty fields, the summary line still fires but
    all ``*_updated`` flags must be False.

    This is the signal we use to detect "compaction ran but found nothing
    to persist" -- which is fine occasionally but persistent emptiness
    points to an upstream issue (agent isn't surfacing facts in the
    conversation, or the prompt isn't extracting them).
    """
    import logging

    mock_response = make_text_response(
        json.dumps(
            {"memory_update": "", "summary": "", "user_profile_update": "", "soul_update": ""}
        )
    )

    messages: list[AgentMessage] = [UserMessage(content="just a chat")]

    with (
        caplog.at_level(logging.INFO, logger="backend.app.agent.compaction"),
        patch("backend.app.agent.compaction.amessages", return_value=mock_response),
    ):
        await compact_session(test_user.id, messages)

    summary_lines = [r for r in caplog.records if "compaction.summary" in r.getMessage()]
    assert len(summary_lines) == 1
    msg = summary_lines[0].getMessage()
    assert "memory_updated=False" in msg
    assert "user_updated=False" in msg
    assert "soul_updated=False" in msg
    assert "summary_len=0" in msg


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
        mock_settings.compaction_event_snapshot_max_bytes_per_file = 100_000
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
        mock_settings.compaction_event_snapshot_max_bytes_per_file = 100_000
        mock_settings.llm_model = "test-model"
        mock_settings.llm_provider = "test-provider"
        mock_settings.llm_api_base = None
        await compact_session(test_user.id, messages)

    call_kwargs = mock_llm.call_args
    assert call_kwargs.kwargs.get("model") == "test-model"
    assert call_kwargs.kwargs.get("provider") == "test-provider"


# --- compact_session llm_usage_logs tests ---


@pytest.mark.asyncio()
async def test_compact_session_logs_llm_usage(test_user: UserData) -> None:
    """Successful compaction should record a llm_usage_logs row with purpose='compaction'."""
    mock_response = make_text_response(json.dumps({"memory_update": "", "summary": ""}))

    messages: list[AgentMessage] = [UserMessage(content="hello")]

    with (
        patch("backend.app.agent.compaction.amessages", return_value=mock_response),
        patch("backend.app.agent.compaction.log_llm_usage") as mock_log,
        patch("backend.app.agent.compaction.settings") as mock_settings,
    ):
        mock_settings.compaction_enabled = True
        mock_settings.compaction_model = "test-compact-model"
        mock_settings.compaction_provider = "test-provider"
        mock_settings.compaction_max_tokens = 300
        mock_settings.compaction_event_snapshot_max_bytes_per_file = 100_000
        mock_settings.llm_model = "test-model"
        mock_settings.llm_provider = "test-provider"
        mock_settings.llm_api_base = None
        await compact_session(test_user.id, messages)

    mock_log.assert_called_once()
    args, kwargs = mock_log.call_args
    assert args[0] == test_user.id
    assert args[1] == "test-compact-model"
    assert args[2] is mock_response
    assert kwargs.get("purpose") == "compaction"


@pytest.mark.asyncio()
async def test_compact_session_does_not_log_when_llm_fails(test_user: UserData) -> None:
    """A failed amessages call should not emit a usage log row."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    with (
        patch(
            "backend.app.agent.compaction.amessages",
            side_effect=Exception("LLM unavailable"),
        ),
        patch("backend.app.agent.compaction.log_llm_usage") as mock_log,
    ):
        await compact_session(test_user.id, messages)

    mock_log.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_does_not_log_when_disabled(test_user: UserData) -> None:
    """When compaction is disabled, no usage log row should be written."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    with (
        patch("backend.app.agent.compaction.settings") as mock_settings,
        patch("backend.app.agent.compaction.amessages") as mock_llm,
        patch("backend.app.agent.compaction.log_llm_usage") as mock_log,
    ):
        mock_settings.compaction_enabled = False
        await compact_session(test_user.id, messages)

    mock_llm.assert_not_called()
    mock_log.assert_not_called()


@pytest.mark.asyncio()
async def test_compact_session_does_not_log_when_no_messages(test_user: UserData) -> None:
    """An empty message list should short-circuit before any usage log is written."""
    with (
        patch("backend.app.agent.compaction.amessages") as mock_llm,
        patch("backend.app.agent.compaction.log_llm_usage") as mock_log,
    ):
        await compact_session(test_user.id, [])

    mock_llm.assert_not_called()
    mock_log.assert_not_called()


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
    from backend.app.agent import context as _context_module
    from backend.app.agent.context import trigger_compaction_for_dropped

    # Dropped messages must carry seq so the trigger can advance the
    # per-session watermark in the same transaction as the pending event
    # row insert. Without seq, the trigger is a no-op (in-memory
    # placeholders cannot pin a DB row range).
    dropped: list[AgentMessage] = [
        UserMessage(content="Old message 1", seq=1),
        AssistantMessage(content="Old reply 1", seq=2),
    ]

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Facts\n- fact: from_trim", "summary": ""})
    )

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await trigger_compaction_for_dropped(test_user.id, dropped)
        # Wait deterministically for the background task to finish rather
        # than sleeping a fixed window. ``-n auto`` workers contend for CPU
        # and a 200ms sleep flakes; gather() resolves as soon as the
        # compaction task completes.
        if _context_module._background_tasks:
            await asyncio.gather(*list(_context_module._background_tasks), return_exceptions=True)

    store = get_memory_store(test_user.id)
    content = await store.read_memory_async()
    assert "fact: from_trim" in content


@pytest.mark.asyncio()
async def test_trigger_compaction_for_dropped_skips_empty(
    test_user: UserData,
) -> None:
    """trigger_compaction_for_dropped should do nothing with empty dropped list."""
    from backend.app.agent.context import trigger_compaction_for_dropped

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        await trigger_compaction_for_dropped(test_user.id, [])
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
        await trigger_compaction_for_dropped(test_user.id, dropped)
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
    history_content = await memory_store.read_history_async()
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
    assert await memory_store.read_history_async() == ""


@pytest.mark.asyncio()
async def test_concurrent_get_or_create_session_does_not_duplicate() -> None:
    """Two concurrent get_or_create_session calls for the same user must
    converge on the same session row.

    The schema enforces ``UNIQUE(user_id)`` on ``sessions``, but the
    advisory lock in ``get_or_create_session`` is what makes the
    runner-up gracefully see the winner's row instead of raising an
    IntegrityError.
    """
    db = open_test_db_session()
    try:
        user = User(
            user_id="concurrent-session-race",
            phone="+15550008888",
            channel_identifier="888",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)

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

    db = open_test_db_session()
    try:
        from backend.app.models import ChatSession as CS

        sessions = (await db.execute(select(CS).filter_by(user_id=user.id))).scalars().all()
        session_count = len(sessions)

    assert session_count == 1, f"expected 1 session, got {session_count}"


# --- CompactionEvent persistence ---


@pytest.mark.asyncio()
async def test_compact_session_writes_event_row(test_user: UserData) -> None:
    """Every successful compaction must leave one CompactionEvent row.

    Pins the regression we just fixed: pre-this-PR the metrics were
    INFO-logged only, so admins debugging "when did this user last
    compact" had to grep Railway. Now they can query.
    """
    from backend.app.models import CompactionEvent

    llm_response_content = json.dumps(
        {
            "memory_update": "## Notes\n- prefers terse replies",
            "user_profile_update": "Likes pizza.",
            "summary": "[TIMESTAMP] talked about lunch",
        }
    )
    mock_response = make_text_response(llm_response_content)

    messages: list[AgentMessage] = [
        UserMessage(content="i like pizza"),
        AssistantMessage(content="noted"),
    ]

    db = open_test_db_session()
    try:
        before = db.query(CompactionEvent).filter_by(user_id=test_user.id).count()
    finally:
        db.close()

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, messages, max_message_seq=2)

    db = open_test_db_session()
    try:
        rows = (
            (
                await db.execute(
                    select(CompactionEvent)
                    .filter_by(user_id=test_user.id)
                    .order_by(CompactionEvent.id.desc())
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == before + 1
    row = rows[0]
    assert row.trimmed_count == 2
    assert row.trimmed_chars > 0
    assert row.duration_ms >= 0
    assert row.max_message_seq == 2
    assert row.memory_updated is True
    assert row.user_profile_updated is True
    assert row.soul_updated is False
    assert row.summary_len > 0
    assert row.triggered_at is not None


@pytest.mark.asyncio()
async def test_compact_session_db_failure_does_not_fail_compaction(
    test_user: UserData,
) -> None:
    """If the event-row write throws, compaction must still succeed.

    The memory write happened upstream of the persistence call; we must
    not fail the whole compaction (and lose the memory_update return
    value the caller is about to act on) just because audit-style
    persistence broke.
    """
    llm_response_content = json.dumps({"memory_update": "## A\n- b", "summary": "[TIMESTAMP] x"})
    mock_response = make_text_response(llm_response_content)
    messages: list[AgentMessage] = [UserMessage(content="hi")]

    with (
        patch("backend.app.agent.compaction.amessages", return_value=mock_response),
        patch(
            "backend.app.agent.compaction._persist_compaction_event",
            side_effect=RuntimeError("db down"),
        ),
    ):
        memory_update, max_seq = await compact_session(test_user.id, messages, max_message_seq=1)

    assert "## A" in memory_update
    assert max_seq == 1


# ---------------------------------------------------------------------------
# Snapshot serialization (truncation cap + skip-if-unchanged)
# ---------------------------------------------------------------------------


def test_serialize_snapshot_under_cap_returns_text() -> None:
    """Plaintext within the cap passes through unchanged."""
    text = "small content"
    assert _serialize_snapshot(text, cap=10_000) == text


def test_serialize_snapshot_none_returns_none() -> None:
    assert _serialize_snapshot(None, cap=10_000) is None


def test_serialize_snapshot_over_cap_returns_truncation_record() -> None:
    """Plaintext over the cap returns a structured truncation record so the
    row size stays bounded regardless of how large MEMORY.md grows.
    """
    big = "x" * 50_000
    out = _serialize_snapshot(big, cap=10_000)
    assert out is not None
    record = json.loads(out)
    assert record["truncated"] is True
    assert record["size_bytes"] == 50_000
    assert len(record["sha256"]) == 64  # sha256 hex digest length
    # Head and tail are bounded by their internal sizes (2KB each in module).
    assert len(record["head"]) <= 2_000
    assert len(record["tail"]) <= 2_000


def test_build_snapshot_pairs_skips_unchanged() -> None:
    """Files with before == after produce None for both columns; the persist
    path will leave both columns NULL, saving encryption + storage overhead.
    """
    pairs = _build_snapshot_pairs(
        cap=10_000,
        memory_before="same memory",
        memory_after="same memory",
        history_before="old history",
        history_after="old history\nnew entry",
        user_before="same user",
        user_after="same user",
        soul_before="",
        soul_after="",
    )
    assert pairs["memory_text_before"] is None
    assert pairs["memory_text_after"] is None
    assert pairs["history_text_before"] == "old history"
    assert pairs["history_text_after"] == "old history\nnew entry"
    assert pairs["user_text_before"] is None
    assert pairs["user_text_after"] is None
    assert pairs["soul_text_before"] is None
    assert pairs["soul_text_after"] is None


# ---------------------------------------------------------------------------
# load_conversation_history watermark filter
# ---------------------------------------------------------------------------


async def _seed_session_with_messages(user: User, message_count: int) -> ChatSession:
    """Insert a ChatSession for *user* with *message_count* alternating
    inbound/outbound messages, returning the persisted ChatSession.
    """
    db = open_test_db_session()
    try:
        cs = ChatSession(
            session_id=f"session-{user.id}",
            user_id=user.id,
            channel="webchat",
            initial_system_prompt="",
        )
        db.add(cs)
        await db.flush()
        for i in range(1, message_count + 1):
            from backend.app.models import Message

            db.add(
                Message(
                    session_id=cs.id,
                    seq=i,
                    direction=(
                        MessageDirection.INBOUND if i % 2 == 1 else MessageDirection.OUTBOUND
                    ),
                    body=f"msg {i}",
                    processed_context="",
                    tool_interactions_json="",
                    external_message_id="",
                    media_urls_json="[]",
                )
            )
        await db.commit()
        await db.refresh(cs)
        db.expunge(cs)
        return cs


@pytest.mark.asyncio()
async def test_load_conversation_history_respects_last_trim_seq(test_user: User) -> None:
    """Messages with seq <= last_trim_seq must be filtered out."""
    cs = _seed_session_with_messages(test_user, message_count=20)
    db = open_test_db_session()
    try:
        cs_ref = db.query(ChatSession).filter_by(id=cs.id).first()
        assert cs_ref is not None
        cs_ref.last_trim_seq = 10
        await db.commit()

    session = await get_session_store(test_user.id).load_session_async(cs.session_id)
    assert session is not None
    history = await load_conversation_history(session)

    # Excludes the most recent (current message). Filtered to seq > 10.
    # So we keep seqs 11..19 (the last one, seq=20, is the "current" excluded).
    contents = {getattr(m, "content", None) for m in history}
    assert "msg 1" not in contents
    assert "msg 10" not in contents
    assert "msg 11" in contents
    assert "msg 19" in contents


@pytest.mark.asyncio()
async def test_load_conversation_history_null_watermark_no_filter(test_user: User) -> None:
    """NULL watermark (default) is the back-compat behavior: no filtering."""
    cs = await _seed_session_with_messages(test_user, message_count=10)
    session = await get_session_store(test_user.id).load_session_async(cs.session_id)
    assert session is not None
    assert session.last_trim_seq is None

    history = await load_conversation_history(session)
    contents = {getattr(m, "content", None) for m in history}
    # All non-current messages present.
    assert "msg 1" in contents
    assert "msg 9" in contents


# ---------------------------------------------------------------------------
# trigger_compaction_for_dropped: synchronous pre-insert + watermark advance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_trigger_compaction_for_dropped_no_seqs_skips(test_user: User) -> None:
    """When no dropped messages carry seq (all in-memory placeholders),
    trigger_compaction must NOT insert an event or advance the watermark.
    """
    in_memory_only: list[AgentMessage] = [
        UserMessage(content="placeholder"),  # seq=None
    ]
    await trigger_compaction_for_dropped(test_user.id, in_memory_only)
    db = open_test_db_session()
    try:
        rows = db.query(CompactionEvent).filter_by(user_id=test_user.id).count()
    finally:
        db.close()
    assert rows == 0


@pytest.mark.asyncio()
async def test_trigger_compaction_for_dropped_inserts_pending_and_advances_watermark(
    test_user: User,
) -> None:
    """The synchronous phase must insert a 'pending' CompactionEvent row
    AND advance sessions.last_trim_seq to max(dropped seqs), atomically.
    """
    cs = await _seed_session_with_messages(test_user, message_count=20)
    dropped: list[AgentMessage] = [
        UserMessage(content="m1", seq=3),
        AssistantMessage(content="r1", seq=4),
        UserMessage(content="m2", seq=5),
    ]
    # Mock the async compaction call so the test asserts only the
    # synchronous behavior.
    with patch(
        "backend.app.agent.context.compact_session",
        new=AsyncMock(return_value=("", 5)),
    ):
        await trigger_compaction_for_dropped(test_user.id, dropped)
        # Yield to let the (mocked) background task run.
        await asyncio.sleep(0)

    db = open_test_db_session()
    try:
        cs_ref = db.query(ChatSession).filter_by(id=cs.id).first()
        assert cs_ref is not None
        assert cs_ref.last_trim_seq == 5

        events = (
            (await db.execute(select(CompactionEvent).filter_by(user_id=test_user.id)))
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.min_message_seq == 3
        assert event.max_message_seq == 5
        # Status is whatever the (mocked) compaction left it at; with our
        # AsyncMock, _persist_compaction_event was not called, so the row
        # stays at the synchronously-inserted 'pending'.
        assert event.status == "pending"


@pytest.mark.asyncio()
async def test_watermark_event_seq_invariant_after_compaction(test_user: User) -> None:
    """After a successful compaction, sessions.last_trim_seq must equal
    compaction_events.max_message_seq for that event's row.
    """
    cs = await _seed_session_with_messages(test_user, message_count=20)
    dropped: list[AgentMessage] = [
        UserMessage(content="m", seq=7),
        AssistantMessage(content="r", seq=8),
    ]
    with patch(
        "backend.app.agent.context.compact_session",
        new=AsyncMock(return_value=("", 8)),
    ):
        await trigger_compaction_for_dropped(test_user.id, dropped)
        await asyncio.sleep(0)

    db = open_test_db_session()
    try:
        cs_ref = db.query(ChatSession).filter_by(id=cs.id).first()
        event = db.query(CompactionEvent).filter_by(user_id=test_user.id).first()
        assert cs_ref is not None
        assert event is not None
        assert cs_ref.last_trim_seq == event.max_message_seq == 8


# ---------------------------------------------------------------------------
# compact_session with event_id: UPDATE the pre-inserted row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_compact_session_with_event_id_updates_existing_row(
    test_user: User,
) -> None:
    """When event_id is provided, compact_session must UPDATE that row
    (flip status to 'completed', fill in snapshots) instead of inserting.
    """
    db = open_test_db_session()
    try:
        ev = CompactionEvent(
            user_id=test_user.id,
            status="pending",
            min_message_seq=1,
            max_message_seq=5,
            trimmed_count=2,
        )
        db.add(ev)
        await db.commit()
        await db.refresh(ev)
        event_id = ev.id

    # Seed memory before so the LLM-driven write produces a diff.
    memory_store = get_memory_store(test_user.id)
    await memory_store.write_memory_async("# Old MEMORY\n- nothing here yet")

    llm_response = json.dumps(
        {"memory_update": "# New MEMORY\n- learned something", "summary": "[TIMESTAMP] s"}
    )
    mock_response = make_text_response(llm_response)
    dropped: list[AgentMessage] = [
        UserMessage(content="hello world", seq=4),
        AssistantMessage(content="hello back", seq=5),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, dropped, max_message_seq=5, event_id=event_id)

    db = open_test_db_session()
    try:
        ev = db.query(CompactionEvent).filter_by(id=event_id).first()
        assert ev is not None
        assert ev.status == "completed"
        # Memory changed, so before/after columns are populated.
        assert ev.memory_text_before == "# Old MEMORY\n- nothing here yet"
        assert ev.memory_text_after is not None
        assert "learned something" in ev.memory_text_after
        # Total compaction events: still exactly one (UPDATE, not INSERT).
        all_events_rows = (
            (await db.execute(select(CompactionEvent).filter_by(user_id=test_user.id)))
            .scalars()
            .all()
        )
        assert len(all_events_rows) == 1


# ---------------------------------------------------------------------------
# Migration 031 capture (prompt / raw response / parsed fields)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_compact_session_captures_llm_prompt_raw_and_parsed(
    test_user: User,
) -> None:
    """compact_session must populate prompt_text, raw_response_text, and
    parsed_response_json on the persisted row so admins can answer 'why
    did the LLM only update some files?' from the UI rather than from
    raw-SQL spelunking. The parsed JSON must round-trip the four
    CompactionResult fields including empty strings for files the LLM
    chose not to update.
    """
    raw_llm_text = json.dumps(
        {
            "memory_update": "# New MEMORY\n- learned",
            "summary": "[TIMESTAMP] s",
            # Mirrors the prod observation that motivated #414: the LLM
            # routinely returns empty user_profile_update / soul_update.
            "user_profile_update": "",
            "soul_update": "",
        }
    )
    mock_response = make_text_response(raw_llm_text)
    dropped: list[AgentMessage] = [
        UserMessage(content="tell me about the new project", seq=1),
        AssistantMessage(content="sure, here is the plan", seq=2),
    ]

    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        await compact_session(test_user.id, dropped, max_message_seq=2)

    db = open_test_db_session()
    try:
        ev = (
            (
                await db.execute(
                    select(CompactionEvent)
                    .filter_by(user_id=test_user.id)
                    .order_by(CompactionEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert ev is not None
        # Prompt is the trimmed conversation block exactly. The static
        # system prompt and the four current-memory inputs are NOT in
        # this column (system is invariant across events; current
        # memory is already in the *_text_before snapshots).
        assert ev.prompt_text is not None
        assert "tell me about the new project" in ev.prompt_text
        assert "sure, here is the plan" in ev.prompt_text
        # Raw response is the unparsed model output.
        assert ev.raw_response_text == raw_llm_text
        # Parsed JSON round-trips all four fields, including empties so
        # the UI can render "(empty, file unchanged)" instead of hiding
        # them.
        assert ev.parsed_response_json is not None
        parsed = json.loads(ev.parsed_response_json)
        assert parsed["memory_update"] == "# New MEMORY\n- learned"
        assert parsed["summary"] == "[TIMESTAMP] s"
        assert parsed["user_profile_update"] == ""
        assert parsed["soul_update"] == ""


@pytest.mark.asyncio()
async def test_compact_session_truncates_oversized_prompt(
    test_user: User,
) -> None:
    """A conversation that exceeds the per-file truncation cap must
    write a structured truncation record into prompt_text rather than
    blowing up the row. The cap is shared with the snapshot columns
    from migration 030.
    """
    huge_user_message = "x" * 60_000
    raw_llm_text = json.dumps({"memory_update": "ok", "summary": "[TIMESTAMP]"})
    mock_response = make_text_response(raw_llm_text)
    dropped: list[AgentMessage] = [
        UserMessage(content=huge_user_message, seq=1),
    ]

    with (
        patch("backend.app.agent.compaction.amessages", return_value=mock_response),
        patch.object(
            settings,
            "compaction_event_snapshot_max_bytes_per_file",
            10_000,
        ),
    ):
        await compact_session(test_user.id, dropped, max_message_seq=1)

    db = open_test_db_session()
    try:
        ev = (
            (
                await db.execute(
                    select(CompactionEvent)
                    .filter_by(user_id=test_user.id)
                    .order_by(CompactionEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert ev is not None
        assert ev.prompt_text is not None
        record = json.loads(ev.prompt_text)
        assert record["truncated"] is True
        assert record["size_bytes"] >= 60_000
