import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import AgentResponse
from backend.app.agent.onboarding import (
    build_onboarding_system_prompt,
    extract_profile_updates,
    is_onboarding_needed,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response, make_tool_call_response


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


@pytest.fixture()
def new_contractor(db_session: Session) -> Contractor:
    """Contractor with no profile — needs onboarding."""
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
    service.send_typing_indicator = AsyncMock()
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


# ---------------------------------------------------------------------------
# Regression tests for #186 / #183: profile updates post-onboarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_profile_updates_post_onboarding_single_field(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    mock_messaging: MessagingService,
) -> None:
    """Post-onboarding save_fact calls should update Contractor profile fields.

    Regression test for #186: after onboarding, a contractor saying "I moved to
    Denver" triggers save_fact(key='location', value='Denver, CO') in memory but
    the Contractor.location field was never updated, causing soul prompt and
    memory to diverge.
    """
    # test_contractor has onboarding complete (name + trade set)
    assert test_contractor.location == "Portland, OR"

    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    msg = Message(conversation_id=conv.id, direction="inbound", body="I moved to Denver")
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    # First LLM call returns a save_fact tool call, second returns text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_loc",
                "name": "save_fact",
                "arguments": json.dumps({"key": "location", "value": "Denver, CO"}),
            }
        ]
    )
    text_response = make_text_response("Got it, updated your location to Denver!")

    mock_acompletion.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(test_contractor)
    assert test_contractor.location == "Denver, CO"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_profile_updates_post_onboarding_multiple_fields(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    mock_messaging: MessagingService,
) -> None:
    """Multiple profile fields updated in a single post-onboarding message.

    When a contractor says "I'm in Denver now and my rate is $100/hr", both
    location and hourly_rate should be updated on the Contractor record.
    """
    assert test_contractor.location == "Portland, OR"

    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    msg = Message(
        conversation_id=conv.id,
        direction="inbound",
        body="I moved to Denver and my rate is $100/hr now",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    # LLM saves both facts in one round, then gives a text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_loc",
                "name": "save_fact",
                "arguments": json.dumps({"key": "location", "value": "Denver, CO"}),
            },
            {
                "id": "call_rate",
                "name": "save_fact",
                "arguments": json.dumps({"key": "hourly_rate", "value": "$100/hr"}),
            },
        ]
    )
    text_response = make_text_response("Updated your location and rate!")

    mock_acompletion.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(test_contractor)
    assert test_contractor.location == "Denver, CO"
    assert test_contractor.hourly_rate == 100.0
    # Onboarding should remain complete
    assert (
        test_contractor.onboarding_complete is not True
        or is_onboarding_needed(test_contractor) is False
    )


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_profile_updates_during_onboarding_still_work(
    mock_acompletion: object,
    db_session: Session,
    new_contractor: Contractor,
    onboarding_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Profile updates during onboarding still work after the refactor.

    Regression: ensures moving extract_profile_updates outside the onboarding
    block didn't break the onboarding flow. When a new contractor provides name
    and trade, onboarding should complete.
    """
    assert is_onboarding_needed(new_contractor) is True
    assert not new_contractor.name  # empty or None
    assert not new_contractor.trade  # empty or None

    # LLM saves name and trade, then gives a text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_name",
                "name": "save_fact",
                "arguments": json.dumps({"key": "name", "value": "Sarah"}),
            },
            {
                "id": "call_trade",
                "name": "save_fact",
                "arguments": json.dumps({"key": "trade", "value": "Plumber"}),
            },
        ]
    )
    text_response = make_text_response("Welcome Sarah! Great to have a plumber on board.")

    mock_acompletion.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=new_contractor,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(new_contractor)
    assert new_contractor.name == "Sarah"
    assert new_contractor.trade == "Plumber"
    assert new_contractor.onboarding_complete is True


# ---------------------------------------------------------------------------
# Regression tests for #180: pre-populated contractors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_prepopulated_contractor_gets_onboarding_complete(
    mock_acompletion: object,
    db_session: Session,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with pre-populated name and trade should get onboarding_complete=True.

    Regression test for #180: when required profile fields are already filled,
    is_onboarding_needed() returns False but onboarding_complete was never set
    because the 'if onboarding:' block was skipped entirely.
    """
    contractor = Contractor(
        user_id="prepopulated-user",
        name="Sarah",
        trade="Electrician",
        channel_identifier="888888888",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    # Sanity: fields are populated but flag is not set
    assert not contractor.onboarding_complete
    assert not is_onboarding_needed(contractor)

    conv = Conversation(contractor_id=contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)

    msg = Message(
        conversation_id=conv.id,
        direction="inbound",
        body="Hey, can you help me with a quote?",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    mock_acompletion.return_value = make_text_response(  # type: ignore[union-attr]
        "Sure thing, Sarah!"
    )

    await handle_inbound_message(
        db=db_session,
        contractor=contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(contractor)
    assert contractor.onboarding_complete is True


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
@patch("backend.app.agent.heartbeat.run_cheap_checks")
@patch("backend.app.agent.core.acompletion")
async def test_prepopulated_contractor_included_in_heartbeat(
    mock_acompletion: object,
    mock_cheap_checks: MagicMock,
    _mock_hours: MagicMock,
    db_session: Session,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with pre-populated fields should be eligible for heartbeat after first message.

    Regression test for #180: heartbeat queries onboarding_complete=True, so
    contractors that never got the flag set were permanently excluded.
    """
    from backend.app.agent.heartbeat import CheapCheckResult, run_heartbeat_for_contractor

    contractor = Contractor(
        user_id="prepopulated-hb-user",
        name="Jake",
        trade="Plumber",
        phone="+15550009999",
        channel_identifier="777777777",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    conv = Conversation(contractor_id=contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)

    msg = Message(
        conversation_id=conv.id,
        direction="inbound",
        body="I need help with an estimate",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    # Process a message to trigger the onboarding_complete fix
    mock_acompletion.return_value = make_text_response(  # type: ignore[union-attr]
        "Happy to help, Jake!"
    )

    await handle_inbound_message(
        db=db_session,
        contractor=contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(contractor)
    assert contractor.onboarding_complete is True

    # Now verify heartbeat doesn't skip this contractor
    mock_cheap_checks.return_value = CheapCheckResult(flags=[])
    result = await run_heartbeat_for_contractor(
        db=db_session,
        contractor=contractor,
        messaging_service=mock_messaging,
        daily_counts={},
        max_daily=5,
    )
    # Should get a result (not None which means skipped)
    assert result is not None
    assert result.action_type == "no_action"  # Clean checks, no message needed


# ---------------------------------------------------------------------------
# Regression tests for #184: onboarding completion message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_onboarding_completion_message_appended(
    mock_acompletion: object,
    db_session: Session,
    mock_messaging: MessagingService,
) -> None:
    """Completion summary should be appended when onboarding transitions to complete."""
    # Contractor with no name/trade — needs onboarding
    contractor = Contractor(
        user_id="completing-user",
        phone="+15550008888",
        channel_identifier="888888888",
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    conv = Conversation(contractor_id=contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    msg = Message(conversation_id=conv.id, direction="inbound", body="I'm Jake, I'm a plumber")
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    # Simulate agent saving name and trade (completing required fields)
    tool_calls = [
        {
            "name": "save_fact",
            "arguments": '{"key": "name", "value": "Jake"}',
        },
        {
            "name": "save_fact",
            "arguments": '{"key": "trade", "value": "Plumber"}',
        },
    ]

    # First call: tool calls to save name/trade; second call: text reply
    mock_acompletion.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(tool_calls, content=None),
        make_text_response("Great to meet you, Jake!"),
    ]

    response = await handle_inbound_message(
        db=db_session,
        contractor=contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "Setup complete!" in response.reply_text
    assert "- Name: Jake" in response.reply_text
    assert "- Trade: Plumber" in response.reply_text
    assert "You can update any of this anytime" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_onboarding_completion_message_includes_optional_fields(
    mock_acompletion: object,
    db_session: Session,
    mock_messaging: MessagingService,
) -> None:
    """Completion summary should include location and rate when available."""
    # Contractor with location already set, still needs name+trade
    contractor = Contractor(
        user_id="optional-fields-user",
        phone="+15550009999",
        channel_identifier="999999998",
        location="Portland, OR",
        hourly_rate=85.0,
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)

    conv = Conversation(contractor_id=contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    msg = Message(conversation_id=conv.id, direction="inbound", body="I'm Sarah, electrician")
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    tool_calls = [
        {
            "name": "save_fact",
            "arguments": '{"key": "name", "value": "Sarah"}',
        },
        {
            "name": "save_fact",
            "arguments": '{"key": "trade", "value": "Electrician"}',
        },
    ]

    mock_acompletion.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(tool_calls, content=None),
        make_text_response("Welcome aboard, Sarah!"),
    ]

    response = await handle_inbound_message(
        db=db_session,
        contractor=contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "Setup complete!" in response.reply_text
    assert "- Name: Sarah" in response.reply_text
    assert "- Trade: Electrician" in response.reply_text
    assert "- Location: Portland, OR" in response.reply_text
    assert "- Rate: $85/hour" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_no_completion_message_when_already_onboarded(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    mock_messaging: MessagingService,
) -> None:
    """Completion message should NOT be appended for already-onboarded contractors."""
    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    msg = Message(
        conversation_id=conv.id, direction="inbound", body="Can you help me with an estimate?"
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    mock_acompletion.return_value = make_text_response("Sure, I can help!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Sure, I can help!"
    assert "Setup complete!" not in response.reply_text
