import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import (
    ContractorData,
    SessionState,
    StoredMessage,
    get_contractor_store,
)
from backend.app.agent.onboarding import (
    REQUIRED_PROFILE_FIELDS,
    build_onboarding_system_prompt,
    is_onboarding_needed,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response, make_tool_call_response


def _ensure_session_on_disk(contractor: ContractorData, session: SessionState) -> None:
    """Create the contractor directory and session file so file-store writes succeed."""
    cdir = Path(settings.contractor_data_dir) / str(contractor.id)
    sessions_dir = cdir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_path = sessions_dir / f"{session.session_id}.jsonl"
    if not session_path.exists():
        meta = {
            "_type": "metadata",
            "session_id": session.session_id,
            "contractor_id": contractor.id,
            "is_active": session.is_active,
        }
        lines = [json.dumps(meta)]
        for msg in session.messages:
            lines.append(json.dumps(msg.model_dump(), default=str))
        session_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Also write contractor.json so the store can reload the contractor
    contractor_json = cdir / "contractor.json"
    if not contractor_json.exists():
        data = contractor.model_dump()
        data.pop("soul_text", None)
        contractor_json.write_text(json.dumps(data, default=str), encoding="utf-8")


def test_is_onboarding_needed_new_contractor() -> None:
    """New contractor with no name/trade should need onboarding."""
    contractor = ContractorData(id=1, user_id="new-user", phone="+15550001111")
    assert is_onboarding_needed(contractor) is True


def test_is_onboarding_needed_partial_profile() -> None:
    """Contractor with name but no trade still needs onboarding."""
    contractor = ContractorData(id=1, user_id="partial-user", phone="+15550002222", name="Mike")
    assert is_onboarding_needed(contractor) is True


def test_is_onboarding_needed_complete_profile(test_contractor: ContractorData) -> None:
    """Contractor with name, trade, and location does not need onboarding."""
    assert is_onboarding_needed(test_contractor) is False


def test_is_onboarding_needed_respects_flag() -> None:
    """Contractor with onboarding_complete=True should not need onboarding."""
    contractor = ContractorData(
        id=1,
        user_id="flagged-user",
        phone="+15550007777",
        name="",
        trade="",
        onboarding_complete=True,
    )
    assert is_onboarding_needed(contractor) is False


def test_is_onboarding_needed_empty_strings() -> None:
    """Empty strings should still trigger onboarding."""
    contractor = ContractorData(id=1, user_id="empty-user", phone="+15550003333", name="", trade="")
    assert is_onboarding_needed(contractor) is True


def test_required_profile_fields_includes_location() -> None:
    """REQUIRED_PROFILE_FIELDS should include location."""
    assert "location" in REQUIRED_PROFILE_FIELDS


def test_is_onboarding_needed_name_trade_but_no_location() -> None:
    """Contractor with name and trade but no location still needs onboarding."""
    contractor = ContractorData(
        id=1,
        user_id="no-location-user",
        phone="+15550006666",
        name="Jake",
        trade="Plumber",
    )
    assert is_onboarding_needed(contractor) is True


def test_build_onboarding_system_prompt_new_contractor() -> None:
    """Onboarding prompt for new contractor should not include known info."""
    contractor = ContractorData(id=1, user_id="brand-new", phone="+15550004444")

    prompt = build_onboarding_system_prompt(contractor)
    assert "first conversation" in prompt
    assert "new contractor" in prompt
    assert "You already know" not in prompt
    assert "help them with that request FIRST" in prompt


def test_build_onboarding_system_prompt_partial_profile() -> None:
    """Onboarding prompt should include already-known fields."""
    contractor = ContractorData(
        id=1,
        user_id="partial-user",
        phone="+15550005555",
        name="Sarah",
        location="Denver, CO",
    )

    prompt = build_onboarding_system_prompt(contractor)
    assert "You already know" in prompt
    assert "Sarah" in prompt
    assert "Denver" in prompt
    assert "Don't re-ask" in prompt


def test_build_onboarding_system_prompt_includes_assistant_name() -> None:
    """Onboarding prompt should include custom assistant_name in known fields."""
    contractor = ContractorData(
        id=1,
        user_id="named-ai-user",
        phone="+15550006666",
        name="Jake",
        assistant_name="Bolt",
    )

    prompt = build_onboarding_system_prompt(contractor)
    assert "You already know" in prompt
    assert "Bolt" in prompt
    assert "Don't re-ask" in prompt


def test_build_onboarding_prompt_mentions_update_profile() -> None:
    """Onboarding prompt should mention update_profile tool."""
    from backend.app.agent.profile import build_onboarding_prompt

    prompt = build_onboarding_prompt()
    assert "update_profile" in prompt


def test_build_onboarding_prompt_lighthearted_tone() -> None:
    """Onboarding prompt should use lighthearted, conversational tone (issue #446)."""
    from backend.app.agent.profile import build_onboarding_prompt

    prompt = build_onboarding_prompt()
    assert "woke up" in prompt
    assert "Have fun with it" in prompt
    assert "not a form" in prompt


def test_build_onboarding_prompt_mentions_workspace_files() -> None:
    """Onboarding prompt should mention USER.md and SOUL.md for saving info."""
    from backend.app.agent.profile import build_onboarding_prompt

    prompt = build_onboarding_prompt()
    assert "USER.md" in prompt
    assert "SOUL.md" in prompt
    assert "write_file" in prompt


# --- Fixtures ---


@pytest.fixture()
def new_contractor() -> ContractorData:
    """Contractor with no profile -- needs onboarding."""
    return ContractorData(
        id=20,
        user_id="new-user-onboard",
        phone="+15559999999",
        channel_identifier="999999999",
    )


@pytest.fixture()
def onboarding_session(new_contractor: ContractorData) -> SessionState:
    session = SessionState(
        session_id="onboarding-session",
        contractor_id=new_contractor.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="Hey, I heard about Clawbolt",
                seq=1,
            ),
        ],
    )
    _ensure_session_on_disk(new_contractor, session)
    return session


@pytest.fixture()
def onboarding_message() -> StoredMessage:
    return StoredMessage(
        direction="inbound",
        body="Hey, I heard about Clawbolt",
        seq=1,
    )


@pytest.fixture()
def mock_messaging() -> MessagingService:
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="msg_42")
    service.send_media = AsyncMock(return_value="msg_43")
    service.send_message = AsyncMock(return_value="msg_42")
    service.send_typing_indicator = AsyncMock()
    service.download_media = AsyncMock()
    return service


# --- Integration tests ---


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_uses_onboarding_prompt(
    mock_amessages: object,
    new_contractor: ContractorData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """Router should use onboarding prompt for new contractors."""
    mock_amessages.return_value = make_text_response(  # type: ignore[union-attr]
        "Welcome to Clawbolt! What's your name?"
    )

    response = await handle_inbound_message(
        contractor=new_contractor,
        session=onboarding_session,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Welcome to Clawbolt! What's your name?"
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["system"]
    assert "new contractor" in system_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_extracts_profile_updates_via_update_profile(
    mock_amessages: object,
    new_contractor: ContractorData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """Profile updates from update_profile tool should be saved to contractor record."""
    # First call returns update_profile tool call, second returns text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_profile",
                "name": "update_profile",
                "arguments": json.dumps({"name": "Mike"}),
            }
        ]
    )
    text_response = make_text_response("Nice to meet you, Mike!")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        contractor=new_contractor,
        session=onboarding_session,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_contractor_store()
    refreshed = await store.get_by_id(new_contractor.id)
    assert refreshed is not None
    assert refreshed.name == "Mike"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_complete_profile_uses_normal_prompt(
    mock_amessages: object,
    test_contractor: ContractorData,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with complete profile should use normal agent prompt."""
    session = SessionState(
        session_id="test-session",
        contractor_id=test_contractor.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="How much for a deck?", seq=1),
        ],
    )
    message = StoredMessage(direction="inbound", body="How much for a deck?", seq=1)

    mock_amessages.return_value = make_text_response(  # type: ignore[union-attr]
        "Let me help with that estimate!"
    )

    response = await handle_inbound_message(
        contractor=test_contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Let me help with that estimate!"
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["system"]
    assert "new contractor" not in system_msg


# ---------------------------------------------------------------------------
# Regression tests for #186 / #183: profile updates post-onboarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_profile_updates_post_onboarding_single_field(
    mock_amessages: object,
    test_contractor: ContractorData,
    mock_messaging: MessagingService,
) -> None:
    """Post-onboarding update_profile calls should update ContractorData profile fields.

    Regression test for #186: after onboarding, a contractor saying "I moved to
    Denver" triggers update_profile(location="Denver, CO") which directly updates
    the ContractorData record.
    """
    # test_contractor has onboarding complete (name + trade set)
    assert test_contractor.location == "Portland, OR"

    session = SessionState(
        session_id="test-session",
        contractor_id=test_contractor.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="I moved to Denver", seq=1),
        ],
    )
    message = StoredMessage(direction="inbound", body="I moved to Denver", seq=1)

    # First LLM call returns an update_profile tool call, second returns text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_loc",
                "name": "update_profile",
                "arguments": json.dumps({"location": "Denver, CO"}),
            }
        ]
    )
    text_response = make_text_response("Got it, updated your location to Denver!")

    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        contractor=test_contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.location == "Denver, CO"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_profile_updates_post_onboarding_multiple_fields(
    mock_amessages: object,
    test_contractor: ContractorData,
    mock_messaging: MessagingService,
) -> None:
    """Multiple profile fields updated in a single post-onboarding message.

    When a contractor says "I'm in Denver now", location should be updated
    on the ContractorData record via update_profile.
    """
    assert test_contractor.location == "Portland, OR"

    session = SessionState(
        session_id="test-session",
        contractor_id=test_contractor.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="I moved to Denver",
                seq=1,
            ),
        ],
    )
    message = StoredMessage(
        direction="inbound",
        body="I moved to Denver",
        seq=1,
    )

    # LLM updates location via update_profile, then gives a text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_profile",
                "name": "update_profile",
                "arguments": json.dumps({"location": "Denver, CO"}),
            },
        ]
    )
    text_response = make_text_response("Updated your location!")

    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        contractor=test_contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.location == "Denver, CO"
    # Onboarding should remain complete
    assert refreshed.onboarding_complete is not True or is_onboarding_needed(refreshed) is False


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_profile_updates_during_onboarding_still_work(
    mock_amessages: object,
    new_contractor: ContractorData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """Profile updates during onboarding still work with update_profile tool.

    When a new contractor provides name, trade, and location via update_profile,
    onboarding should complete.
    """
    assert is_onboarding_needed(new_contractor) is True
    assert not new_contractor.name  # empty or None
    assert not new_contractor.trade  # empty or None

    # LLM calls update_profile with all required fields, then gives a text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_profile",
                "name": "update_profile",
                "arguments": json.dumps(
                    {
                        "name": "Sarah",
                        "trade": "Plumber",
                        "location": "Austin, TX",
                    }
                ),
            },
        ]
    )
    text_response = make_text_response("Welcome Sarah! Great to have a plumber on board.")

    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        contractor=new_contractor,
        session=onboarding_session,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_contractor_store()
    refreshed = await store.get_by_id(new_contractor.id)
    assert refreshed is not None
    assert refreshed.name == "Sarah"
    assert refreshed.trade == "Plumber"
    assert refreshed.location == "Austin, TX"
    assert refreshed.onboarding_complete is True


# ---------------------------------------------------------------------------
# Regression tests for #180: pre-populated contractors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_contractor_gets_onboarding_complete(
    mock_amessages: object,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with pre-populated name and trade should get onboarding_complete=True.

    Regression test for #180: when required profile fields are already filled,
    is_onboarding_needed() returns False but onboarding_complete was never set
    because the 'if onboarding:' block was skipped entirely.
    """
    contractor = ContractorData(
        id=30,
        user_id="prepopulated-user",
        name="Sarah",
        trade="Electrician",
        location="Austin, TX",
        channel_identifier="888888888",
        preferred_channel="telegram",
        onboarding_complete=False,
    )

    # Sanity: fields are populated but flag is not set
    assert not contractor.onboarding_complete
    assert not is_onboarding_needed(contractor)

    session = SessionState(
        session_id="test-session",
        contractor_id=contractor.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="Hey, can you help me with a quote?",
                seq=1,
            ),
        ],
    )
    message = StoredMessage(
        direction="inbound",
        body="Hey, can you help me with a quote?",
        seq=1,
    )

    mock_amessages.return_value = make_text_response(  # type: ignore[union-attr]
        "Sure thing, Sarah!"
    )
    _ensure_session_on_disk(contractor, session)

    await handle_inbound_message(
        contractor=contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_contractor_store()
    refreshed = await store.get_by_id(contractor.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
@patch("backend.app.agent.heartbeat.run_cheap_checks")
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_contractor_included_in_heartbeat(
    mock_amessages: object,
    mock_cheap_checks: MagicMock,
    _mock_hours: MagicMock,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with pre-populated fields should be eligible for heartbeat after first message.

    Regression test for #180: heartbeat queries onboarding_complete=True, so
    contractors that never got the flag set were permanently excluded.
    """
    from backend.app.agent.heartbeat import CheapCheckResult, run_heartbeat_for_contractor

    contractor = ContractorData(
        id=31,
        user_id="prepopulated-hb-user",
        name="Jake",
        trade="Plumber",
        location="Portland, OR",
        phone="+15550009999",
        channel_identifier="777777777",
        preferred_channel="telegram",
        onboarding_complete=False,
    )

    session = SessionState(
        session_id="test-session",
        contractor_id=contractor.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="I need help with an estimate",
                seq=1,
            ),
        ],
    )
    message = StoredMessage(
        direction="inbound",
        body="I need help with an estimate",
        seq=1,
    )

    # Process a message to trigger the onboarding_complete fix
    mock_amessages.return_value = make_text_response(  # type: ignore[union-attr]
        "Happy to help, Jake!"
    )
    _ensure_session_on_disk(contractor, session)

    await handle_inbound_message(
        contractor=contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_contractor_store()
    refreshed = await store.get_by_id(contractor.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True

    # Now verify heartbeat doesn't skip this contractor
    mock_cheap_checks.return_value = CheapCheckResult(flags=[])
    result = await run_heartbeat_for_contractor(
        contractor=refreshed,
        messaging_service=mock_messaging,
        max_daily=5,
    )
    # Should get a result (not None which means skipped)
    assert result is not None
    assert result.action_type == "no_action"  # Clean checks, no message needed


# ---------------------------------------------------------------------------
# Regression tests for #184: onboarding completion message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_completion_message_appended(
    mock_amessages: object,
    mock_messaging: MessagingService,
) -> None:
    """Completion summary should be appended when onboarding transitions to complete."""
    # Contractor with no name/trade -- needs onboarding
    contractor = ContractorData(
        id=32,
        user_id="completing-user",
        phone="+15550008888",
        channel_identifier="888888888",
    )

    session = SessionState(
        session_id="test-session",
        contractor_id=contractor.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="I'm Jake, I'm a plumber in Portland",
                seq=1,
            ),
        ],
    )
    message = StoredMessage(
        direction="inbound",
        body="I'm Jake, I'm a plumber in Portland",
        seq=1,
    )

    # Simulate agent calling update_profile with all required fields
    tool_calls = [
        {
            "name": "update_profile",
            "arguments": json.dumps(
                {
                    "name": "Jake",
                    "trade": "Plumber",
                    "location": "Portland, OR",
                }
            ),
        },
    ]

    # First call: tool calls to update profile; second call: text reply
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(tool_calls, content=None),
        make_text_response("Great to meet you, Jake!"),
    ]
    _ensure_session_on_disk(contractor, session)

    response = await handle_inbound_message(
        contractor=contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "Setup complete!" in response.reply_text
    assert "- Name: Jake" in response.reply_text
    assert "- Trade: Plumber" in response.reply_text
    assert "- Location: Portland, OR" in response.reply_text
    assert "- Your AI: Clawbolt" in response.reply_text
    assert "You can update any of this anytime" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_completion_message_includes_location(
    mock_amessages: object,
    mock_messaging: MessagingService,
) -> None:
    """Completion summary should include location when available."""
    # Contractor with location already set, still needs name+trade
    contractor = ContractorData(
        id=33,
        user_id="optional-fields-user",
        phone="+15550009999",
        channel_identifier="999999998",
        location="Portland, OR",
    )

    session = SessionState(
        session_id="test-session",
        contractor_id=contractor.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="I'm Sarah, electrician", seq=1),
        ],
    )
    message = StoredMessage(direction="inbound", body="I'm Sarah, electrician", seq=1)

    tool_calls = [
        {
            "name": "update_profile",
            "arguments": json.dumps({"name": "Sarah", "trade": "Electrician"}),
        },
    ]

    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(tool_calls, content=None),
        make_text_response("Welcome aboard, Sarah!"),
    ]
    _ensure_session_on_disk(contractor, session)

    response = await handle_inbound_message(
        contractor=contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "Setup complete!" in response.reply_text
    assert "- Name: Sarah" in response.reply_text
    assert "- Trade: Electrician" in response.reply_text
    assert "- Location: Portland, OR" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_no_completion_message_when_already_onboarded(
    mock_amessages: object,
    test_contractor: ContractorData,
    mock_messaging: MessagingService,
) -> None:
    """Completion message should NOT be appended for already-onboarded contractors."""
    session = SessionState(
        session_id="test-session",
        contractor_id=test_contractor.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="Can you help me with an estimate?",
                seq=1,
            ),
        ],
    )
    message = StoredMessage(
        direction="inbound",
        body="Can you help me with an estimate?",
        seq=1,
    )

    mock_amessages.return_value = make_text_response("Sure, I can help!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        contractor=test_contractor,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Sure, I can help!"
    assert "Setup complete!" not in response.reply_text
