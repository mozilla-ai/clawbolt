import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import (
    SessionState,
    StoredMessage,
    UserData,
    get_user_store,
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


def _ensure_session_on_disk(user: UserData, session: SessionState) -> None:
    """Create the user directory and session file so file-store writes succeed."""
    cdir = Path(settings.data_dir) / str(user.id)
    sessions_dir = cdir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_path = sessions_dir / f"{session.session_id}.jsonl"
    if not session_path.exists():
        meta = {
            "_type": "metadata",
            "session_id": session.session_id,
            "user_id": user.id,
            "is_active": session.is_active,
        }
        lines = [json.dumps(meta)]
        for msg in session.messages:
            lines.append(json.dumps(msg.model_dump(), default=str))
        session_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Also write user.json so the store can reload the user
    user_json = cdir / "user.json"
    if not user_json.exists():
        data = user.model_dump()
        data.pop("soul_text", None)
        user_json.write_text(json.dumps(data, default=str), encoding="utf-8")


def test_is_onboarding_needed_new_user() -> None:
    """New user with no name should need onboarding."""
    user = UserData(id=1, user_id="new-user", phone="+15550001111")
    assert is_onboarding_needed(user) is True


def test_is_onboarding_needed_partial_profile() -> None:
    """User with name should not need onboarding (name is the only required field)."""
    user = UserData(id=1, user_id="partial-user", phone="+15550002222", name="Mike")
    assert is_onboarding_needed(user) is False


def test_is_onboarding_needed_complete_profile(test_user: UserData) -> None:
    """User with name does not need onboarding."""
    assert is_onboarding_needed(test_user) is False


def test_is_onboarding_needed_respects_flag() -> None:
    """User with onboarding_complete=True should not need onboarding."""
    user = UserData(
        id=1,
        user_id="flagged-user",
        phone="+15550007777",
        name="",
        onboarding_complete=True,
    )
    assert is_onboarding_needed(user) is False


def test_is_onboarding_needed_empty_strings() -> None:
    """Empty strings should still trigger onboarding."""
    user = UserData(id=1, user_id="empty-user", phone="+15550003333", name="")
    assert is_onboarding_needed(user) is True


def test_required_profile_fields_only_name() -> None:
    """REQUIRED_PROFILE_FIELDS should only include name."""
    assert "name" in REQUIRED_PROFILE_FIELDS
    assert "trade" not in REQUIRED_PROFILE_FIELDS
    assert "location" not in REQUIRED_PROFILE_FIELDS


def test_build_onboarding_system_prompt_new_user() -> None:
    """Onboarding prompt for new user should not include known info."""
    user = UserData(id=1, user_id="brand-new", phone="+15550004444")

    prompt = build_onboarding_system_prompt(user)
    assert "first conversation" in prompt
    assert "new user" in prompt
    assert "You already know" not in prompt
    assert "help them with that request FIRST" in prompt


def test_build_onboarding_system_prompt_partial_profile() -> None:
    """Onboarding prompt should include already-known fields."""
    user = UserData(
        id=1,
        user_id="partial-user",
        phone="+15550005555",
        name="Sarah",
    )

    prompt = build_onboarding_system_prompt(user)
    assert "You already know" in prompt
    assert "Sarah" in prompt
    assert "Don't re-ask" in prompt


def test_build_onboarding_system_prompt_includes_assistant_name() -> None:
    """Onboarding prompt should include custom assistant_name in known fields."""
    user = UserData(
        id=1,
        user_id="named-ai-user",
        phone="+15550006666",
        name="Jake",
        assistant_name="Bolt",
    )

    prompt = build_onboarding_system_prompt(user)
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


def test_build_onboarding_prompt_mentions_capabilities() -> None:
    """Onboarding prompt should mention capabilities overview."""
    from backend.app.agent.profile import build_onboarding_prompt

    prompt = build_onboarding_prompt()
    assert "capabilities" in prompt.lower()


def test_build_onboarding_system_prompt_includes_tool_capabilities() -> None:
    """Onboarding system prompt should inject available specialist tool descriptions."""
    user = UserData(id=1, user_id="new-user", phone="+15550001111")

    prompt = build_onboarding_system_prompt(user)
    # Should include specialist tool summaries from the registry
    assert "specialist capabilities" in prompt.lower()
    assert "estimate" in prompt.lower()


# --- Fixtures ---


@pytest.fixture()
def new_user() -> UserData:
    """User with no profile -- needs onboarding."""
    return UserData(
        id=20,
        user_id="new-user-onboard",
        phone="+15559999999",
        channel_identifier="999999999",
    )


@pytest.fixture()
def onboarding_session(new_user: UserData) -> SessionState:
    session = SessionState(
        session_id="onboarding-session",
        user_id=new_user.id,
        is_active=True,
        messages=[
            StoredMessage(
                direction="inbound",
                body="Hey, I heard about Clawbolt",
                seq=1,
            ),
        ],
    )
    _ensure_session_on_disk(new_user, session)
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
    new_user: UserData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """Router should use onboarding prompt for new users."""
    mock_amessages.return_value = make_text_response(  # type: ignore[union-attr]
        "Welcome to Clawbolt! What's your name?"
    )

    response = await handle_inbound_message(
        user=new_user,
        session=onboarding_session,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Welcome to Clawbolt! What's your name?"
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["system"]
    assert "new user" in system_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_extracts_profile_updates_via_update_profile(
    mock_amessages: object,
    new_user: UserData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """Profile updates from update_profile tool should be saved to user record."""
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
        user=new_user,
        session=onboarding_session,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_user_store()
    refreshed = await store.get_by_id(new_user.id)
    assert refreshed is not None
    assert refreshed.name == "Mike"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_complete_profile_uses_normal_prompt(
    mock_amessages: object,
    test_user: UserData,
    mock_messaging: MessagingService,
) -> None:
    """User with complete profile should use normal agent prompt."""
    session = SessionState(
        session_id="test-session",
        user_id=test_user.id,
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
        user=test_user,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Let me help with that estimate!"
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["system"]
    assert "new user" not in system_msg


# ---------------------------------------------------------------------------
# Regression tests for #186 / #183: profile updates post-onboarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_profile_updates_post_onboarding_single_field(
    mock_amessages: object,
    test_user: UserData,
    mock_messaging: MessagingService,
) -> None:
    """Post-onboarding update_profile calls should update UserData profile fields."""
    session = SessionState(
        session_id="test-session",
        user_id=test_user.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="My name is now Jake", seq=1),
        ],
    )
    message = StoredMessage(direction="inbound", body="My name is now Jake", seq=1)

    # First LLM call returns an update_profile tool call, second returns text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_name",
                "name": "update_profile",
                "arguments": json.dumps({"name": "Jake"}),
            }
        ]
    )
    text_response = make_text_response("Got it, updated your name to Jake!")

    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_user_store()
    refreshed = await store.get_by_id(test_user.id)
    assert refreshed is not None
    assert refreshed.name == "Jake"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_profile_updates_during_onboarding_still_work(
    mock_amessages: object,
    new_user: UserData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """Profile updates during onboarding still work with update_profile tool.

    When a new user provides name via update_profile,
    onboarding should complete.
    """
    assert is_onboarding_needed(new_user) is True
    assert not new_user.name  # empty or None

    # LLM calls update_profile with name, then gives a text reply
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_profile",
                "name": "update_profile",
                "arguments": json.dumps(
                    {
                        "name": "Sarah",
                    }
                ),
            },
        ]
    )
    text_response = make_text_response("Welcome Sarah!")

    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        user=new_user,
        session=onboarding_session,
        message=onboarding_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_user_store()
    refreshed = await store.get_by_id(new_user.id)
    assert refreshed is not None
    assert refreshed.name == "Sarah"
    assert refreshed.onboarding_complete is True


# ---------------------------------------------------------------------------
# Regression tests for #180: pre-populated users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_user_gets_onboarding_complete(
    mock_amessages: object,
    mock_messaging: MessagingService,
) -> None:
    """User with pre-populated name should get onboarding_complete=True.

    Regression test for #180: when required profile fields are already filled,
    is_onboarding_needed() returns False but onboarding_complete was never set
    because the 'if onboarding:' block was skipped entirely.
    """
    user = UserData(
        id=30,
        user_id="prepopulated-user",
        name="Sarah",
        channel_identifier="888888888",
        preferred_channel="telegram",
        onboarding_complete=False,
    )

    # Sanity: fields are populated but flag is not set
    assert not user.onboarding_complete
    assert not is_onboarding_needed(user)

    session = SessionState(
        session_id="test-session",
        user_id=user.id,
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
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_user_store()
    refreshed = await store.get_by_id(user.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
@patch("backend.app.agent.heartbeat.run_cheap_checks")
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_user_included_in_heartbeat(
    mock_amessages: object,
    mock_cheap_checks: MagicMock,
    _mock_hours: MagicMock,
    mock_messaging: MessagingService,
) -> None:
    """User with pre-populated fields should be eligible for heartbeat after first message."""
    from backend.app.agent.heartbeat import CheapCheckResult, run_heartbeat_for_user

    user = UserData(
        id=31,
        user_id="prepopulated-hb-user",
        name="Jake",
        phone="+15550009999",
        channel_identifier="777777777",
        preferred_channel="telegram",
        onboarding_complete=False,
    )

    session = SessionState(
        session_id="test-session",
        user_id=user.id,
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
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    store = get_user_store()
    refreshed = await store.get_by_id(user.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True

    # Now verify heartbeat doesn't skip this user
    mock_cheap_checks.return_value = CheapCheckResult(flags=[])
    result = await run_heartbeat_for_user(
        user=refreshed,
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
    # User with no name -- needs onboarding
    user = UserData(
        id=32,
        user_id="completing-user",
        phone="+15550008888",
        channel_identifier="888888888",
    )

    session = SessionState(
        session_id="test-session",
        user_id=user.id,
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

    # Simulate agent calling update_profile with name
    tool_calls = [
        {
            "name": "update_profile",
            "arguments": json.dumps(
                {
                    "name": "Jake",
                }
            ),
        },
    ]

    # First call: tool calls to update profile; second call: text reply
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(tool_calls, content=None),
        make_text_response("Great to meet you, Jake!"),
    ]
    _ensure_session_on_disk(user, session)

    response = await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "Setup complete!" in response.reply_text
    assert "- Name: Jake" in response.reply_text
    assert "- Your AI: Clawbolt" in response.reply_text
    assert "You can update any of this anytime" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_no_completion_message_when_already_onboarded(
    mock_amessages: object,
    test_user: UserData,
    mock_messaging: MessagingService,
) -> None:
    """Completion message should NOT be appended for already-onboarded users."""
    session = SessionState(
        session_id="test-session",
        user_id=test_user.id,
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
        user=test_user,
        session=session,
        message=message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Sure, I can help!"
    assert "Setup complete!" not in response.reply_text
