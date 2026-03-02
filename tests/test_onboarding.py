from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import AgentResponse
from backend.app.agent.onboarding import (
    _parse_rate,
    build_onboarding_system_prompt,
    extract_profile_updates,
    is_onboarding_needed,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response


def test_is_onboarding_needed_new_contractor(db_session: Session) -> None:
    """New contractor with no name/trade should need onboarding."""
    contractor = Contractor(user_id="new-user", phone="+15550001111")
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    assert is_onboarding_needed(contractor) is True


def test_is_onboarding_needed_partial_profile(db_session: Session) -> None:
    """Contractor with name but no trade still needs onboarding."""
    contractor = Contractor(user_id="partial-user", phone="+15550002222", name="Mike")
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    assert is_onboarding_needed(contractor) is True


def test_is_onboarding_needed_complete_profile(test_contractor: Contractor) -> None:
    """Contractor with name and trade does not need onboarding."""
    assert is_onboarding_needed(test_contractor) is False


def test_is_onboarding_needed_respects_flag(db_session: Session) -> None:
    """Contractor with onboarding_complete=True should not need onboarding."""
    contractor = Contractor(
        user_id="flagged-user",
        phone="+15550007777",
        name="",
        trade="",
        onboarding_complete=True,
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    assert is_onboarding_needed(contractor) is False


def test_is_onboarding_needed_empty_strings(db_session: Session) -> None:
    """Empty strings should still trigger onboarding."""
    contractor = Contractor(user_id="empty-user", phone="+15550003333", name="", trade="")
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    assert is_onboarding_needed(contractor) is True


def test_build_onboarding_system_prompt_new_contractor(db_session: Session) -> None:
    """Onboarding prompt for new contractor should not include known info."""
    contractor = Contractor(user_id="brand-new", phone="+15550004444")
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    prompt = build_onboarding_system_prompt(contractor)
    assert "Backshop" in prompt
    assert "new contractor" in prompt
    assert "You already know" not in prompt
    # Should include instruction to handle requests alongside onboarding
    assert "help them with that request FIRST" in prompt


def test_build_onboarding_system_prompt_partial_profile(db_session: Session) -> None:
    """Onboarding prompt should include already-known fields."""
    contractor = Contractor(
        user_id="partial-user",
        phone="+15550005555",
        name="Sarah",
        location="Denver, CO",
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    prompt = build_onboarding_system_prompt(contractor)
    assert "You already know" in prompt
    assert "Sarah" in prompt
    assert "Denver" in prompt
    assert "Don't re-ask" in prompt


def test_extract_profile_updates_name_and_trade() -> None:
    """Should extract name and trade from save_fact tool calls."""
    response = AgentResponse(
        reply_text="Nice to meet you!",
        tool_calls=[
            {"name": "save_fact", "args": {"key": "name", "value": "Mike Johnson"}, "result": "ok"},
            {"name": "save_fact", "args": {"key": "trade", "value": "Electrician"}, "result": "ok"},
        ],
    )
    updates = extract_profile_updates(response)
    assert updates["name"] == "Mike Johnson"
    assert updates["trade"] == "Electrician"


def test_extract_profile_updates_hourly_rate() -> None:
    """Should parse numeric hourly rate from save_fact."""
    response = AgentResponse(
        reply_text="Got it!",
        tool_calls=[
            {
                "name": "save_fact",
                "args": {"key": "hourly_rate", "value": "$85/hr"},
                "result": "ok",
            },
        ],
    )
    updates = extract_profile_updates(response)
    assert updates["hourly_rate"] == 85.0


def test_extract_profile_updates_ignores_non_profile_facts() -> None:
    """Should ignore save_fact calls that don't map to profile fields."""
    response = AgentResponse(
        reply_text="Got it!",
        tool_calls=[
            {
                "name": "save_fact",
                "args": {"key": "favorite_color", "value": "blue"},
                "result": "ok",
            },
        ],
    )
    updates = extract_profile_updates(response)
    assert updates == {}


def test_extract_profile_updates_ignores_non_save_fact_tools() -> None:
    """Should only look at save_fact tool calls."""
    response = AgentResponse(
        reply_text="Sent!",
        tool_calls=[
            {"name": "send_reply", "args": {"message": "Hello"}, "result": "ok"},
        ],
    )
    updates = extract_profile_updates(response)
    assert updates == {}


def test_extract_profile_updates_invalid_rate() -> None:
    """Should handle non-numeric rate values gracefully."""
    response = AgentResponse(
        reply_text="Got it!",
        tool_calls=[
            {
                "name": "save_fact",
                "args": {"key": "hourly_rate", "value": "depends on the job"},
                "result": "ok",
            },
        ],
    )
    updates = extract_profile_updates(response)
    assert "hourly_rate" not in updates


# --- _parse_rate unit tests ---


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [
        ("$85/hr", 85.0),
        ("$85/hour", 85.0),
        ("$85 per hour", 85.0),
        ("$85 an hour", 85.0),
        ("85 dollars", 85.0),
        ("$85.50", 85.5),
        ("$85.50/hr", 85.5),
        ("85", 85.0),
        ("85.00", 85.0),
        ("$50-75/hr", 50.0),
        ("$4500 per project", 4500.0),
        ("$4,500 per project", 4500.0),
        ("Usually around $80", 80.0),
        ("$125/hour for electrical", 125.0),
        ("  $65 /hr  ", 65.0),
    ],
)
def test_parse_rate_valid_formats(input_value: str, expected: float) -> None:
    """_parse_rate should extract numeric rate from various natural-language formats."""
    assert _parse_rate(input_value) == expected


@pytest.mark.parametrize(
    "input_value",
    [
        "not sure",
        "varies",
        "depends on the job",
        "TBD",
        "",
    ],
)
def test_parse_rate_invalid_returns_none(input_value: str) -> None:
    """_parse_rate should return None for non-numeric values."""
    assert _parse_rate(input_value) is None


# --- extract_profile_updates with various rate formats ---


@pytest.mark.parametrize(
    ("rate_value", "expected_rate"),
    [
        ("$85/hour", 85.0),
        ("$85 per hour", 85.0),
        ("$85 an hour", 85.0),
        ("85 dollars", 85.0),
        ("$85.50", 85.5),
        ("$50-75/hr", 50.0),
        ("$4,500 per project", 4500.0),
        ("Usually around $80", 80.0),
    ],
)
def test_extract_profile_updates_various_rate_formats(
    rate_value: str, expected_rate: float
) -> None:
    """extract_profile_updates should handle various rate formats via _parse_rate."""
    response = AgentResponse(
        reply_text="Got it!",
        tool_calls=[
            {
                "name": "save_fact",
                "args": {"key": "hourly_rate", "value": rate_value},
                "result": "ok",
            },
        ],
    )
    updates = extract_profile_updates(response)
    assert updates["hourly_rate"] == expected_rate


def test_extract_profile_updates_invalid_rate_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Should log a warning when rate parsing fails."""
    response = AgentResponse(
        reply_text="Got it!",
        tool_calls=[
            {
                "name": "save_fact",
                "args": {"key": "hourly_rate", "value": "varies"},
                "result": "ok",
            },
        ],
    )
    with caplog.at_level("WARNING", logger="backend.app.agent.onboarding"):
        updates = extract_profile_updates(response)
    assert "hourly_rate" not in updates
    assert "Could not parse hourly rate" in caplog.text


@pytest.fixture()
def new_contractor(db_session: Session) -> Contractor:
    """Contractor with no profile -- needs onboarding."""
    contractor = Contractor(
        user_id="new-user-onboard",
        phone="+15559999999",
        channel_identifier="999999999",
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)
    return contractor


@pytest.fixture()
def onboarding_conversation(db_session: Session, new_contractor: Contractor) -> Conversation:
    conv = Conversation(contractor_id=new_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    return conv


@pytest.fixture()
def onboarding_message(db_session: Session, onboarding_conversation: Conversation) -> Message:
    msg = Message(
        conversation_id=onboarding_conversation.id,
        direction="inbound",
        body="Hey, I heard about Backshop",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)
    return msg


@pytest.fixture()
def mock_messaging() -> MessagingService:
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="msg_42")
    service.send_media = AsyncMock(return_value="msg_43")
    service.send_message = AsyncMock(return_value="msg_42")
    return service


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_onboarding_uses_onboarding_prompt(
    mock_acompletion: object,
    db_session: Session,
    new_contractor: Contractor,
    onboarding_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Router should use onboarding prompt for new contractors."""
    mock_acompletion.return_value = make_text_response(  # type: ignore[union-attr]
        "Welcome to Backshop! What's your name?"
    )

    response = await handle_inbound_message(
        db=db_session,
        contractor=new_contractor,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Welcome to Backshop! What's your name?"
    # Verify the system prompt passed was the onboarding prompt
    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["messages"][0]["content"]
    assert "new contractor" in system_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_onboarding_extracts_profile_updates(
    mock_acompletion: object,
    db_session: Session,
    new_contractor: Contractor,
    onboarding_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Profile updates from onboarding should be saved to contractor record."""
    # Simulate agent calling save_fact with name and trade
    resp_mock = make_text_response("Nice to meet you, Mike!")
    tool_call = MagicMock()
    tool_call.function.name = "save_fact"
    tool_call.function.arguments = '{"key": "name", "value": "Mike"}'
    resp_mock.choices[0].message.tool_calls = [tool_call]
    mock_acompletion.return_value = resp_mock  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=new_contractor,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(new_contractor)
    assert new_contractor.name == "Mike"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_complete_profile_uses_normal_prompt(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with complete profile should use normal agent prompt."""
    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    msg = Message(conversation_id=conv.id, direction="inbound", body="How much for a deck?")
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    mock_acompletion.return_value = make_text_response("Let me help with that estimate!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Let me help with that estimate!"
    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["messages"][0]["content"]
    # Normal prompt should NOT contain onboarding text
    assert "new contractor" not in system_msg
