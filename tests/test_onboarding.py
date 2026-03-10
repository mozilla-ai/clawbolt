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
    build_onboarding_system_prompt,
    is_onboarding_needed,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
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


def _create_bootstrap(user: UserData) -> None:
    """Create a BOOTSTRAP.md file for the given user from the real template."""
    from backend.app.agent.prompts import load_prompt

    cdir = Path(settings.data_dir) / str(user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "BOOTSTRAP.md").write_text(load_prompt("bootstrap") + "\n", encoding="utf-8")


def _remove_bootstrap(user: UserData) -> None:
    """Remove BOOTSTRAP.md for the given user."""
    path = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    if path.exists():
        path.unlink()


def test_is_onboarding_needed_new_user() -> None:
    """New user with BOOTSTRAP.md should need onboarding."""
    user = UserData(id=1, user_id="new-user", phone="+15550001111")
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True


def test_is_onboarding_needed_no_bootstrap() -> None:
    """User without BOOTSTRAP.md should not need onboarding."""
    user = UserData(id=2, user_id="no-bootstrap-user", phone="+15550002222")
    # Ensure user dir exists but no BOOTSTRAP.md
    cdir = Path(settings.data_dir) / str(user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    assert is_onboarding_needed(user) is False


def test_is_onboarding_needed_complete_profile(test_user: UserData) -> None:
    """User with onboarding_complete=True does not need onboarding."""
    assert is_onboarding_needed(test_user) is False


def test_is_onboarding_needed_respects_flag() -> None:
    """User with onboarding_complete=True should not need onboarding even with BOOTSTRAP.md."""
    user = UserData(
        id=3,
        user_id="flagged-user",
        phone="+15550007777",
        onboarding_complete=True,
    )
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is False


def test_is_onboarding_needed_bootstrap_deleted() -> None:
    """After BOOTSTRAP.md is deleted, onboarding is not needed."""
    user = UserData(id=4, user_id="deleted-bootstrap-user", phone="+15550003333")
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True
    _remove_bootstrap(user)
    assert is_onboarding_needed(user) is False


def test_build_onboarding_system_prompt_new_user() -> None:
    """Onboarding prompt for new user should include bootstrap content."""
    user = UserData(id=5, user_id="brand-new", phone="+15550004444")
    _create_bootstrap(user)

    prompt = build_onboarding_system_prompt(user)
    assert "help them with that request FIRST" in prompt


def test_build_onboarding_system_prompt_includes_tool_capabilities() -> None:
    """Onboarding system prompt should inject available specialist tool descriptions."""
    user = UserData(id=6, user_id="new-user", phone="+15550001111")
    _create_bootstrap(user)

    prompt = build_onboarding_system_prompt(user)
    # Should include specialist tool summaries from the registry
    assert "specialist capabilities" in prompt.lower()
    assert "estimate" in prompt.lower()


# --- Fixtures ---


@pytest.fixture()
def new_user() -> UserData:
    """User with no profile, needs onboarding."""
    user = UserData(
        id=20,
        user_id="new-user-onboard",
        phone="+15559999999",
        channel_identifier="999999999",
    )
    _create_bootstrap(user)
    return user


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
def mock_download_media() -> AsyncMock:
    return AsyncMock()


# --- Integration tests ---


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_uses_onboarding_prompt(
    mock_amessages: object,
    new_user: UserData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
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
        channel="telegram",
    )

    assert response.reply_text == "Welcome to Clawbolt! What's your name?"
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["system"]
    assert "new user" in system_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_completes_when_bootstrap_deleted(
    mock_amessages: object,
    new_user: UserData,
    onboarding_session: SessionState,
    onboarding_message: StoredMessage,
) -> None:
    """Onboarding should complete when BOOTSTRAP.md is deleted via delete_file."""
    assert is_onboarding_needed(new_user) is True

    # Simulate: agent calls write_file to save USER.md, then delete_file to remove BOOTSTRAP.md
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_write",
                "name": "write_file",
                "arguments": json.dumps({"path": "USER.md", "content": "# User\n\n- Name: Sarah"}),
            },
            {
                "id": "call_delete",
                "name": "delete_file",
                "arguments": json.dumps({"path": "BOOTSTRAP.md"}),
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
        channel="telegram",
    )

    store = get_user_store()
    refreshed = await store.get_by_id(new_user.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_complete_profile_uses_normal_prompt(
    mock_amessages: object,
    test_user: UserData,
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
        channel="telegram",
    )

    assert response.reply_text == "Let me help with that estimate!"
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["system"]
    assert "new user" not in system_msg


# ---------------------------------------------------------------------------
# Regression tests for #180: pre-populated users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_user_gets_onboarding_complete(
    mock_amessages: object,
) -> None:
    """User without BOOTSTRAP.md should get onboarding_complete=True.

    Regression test for #180: when BOOTSTRAP.md doesn't exist,
    is_onboarding_needed() returns False but onboarding_complete was never set
    because the 'if onboarding:' block was skipped entirely.
    """
    user = UserData(
        id=30,
        user_id="prepopulated-user",
        channel_identifier="888888888",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    # No BOOTSTRAP.md created, so not onboarding

    # Sanity: flag is not set but onboarding is not needed
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
        "Sure thing!"
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        channel="telegram",
    )

    store = get_user_store()
    refreshed = await store.get_by_id(user.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
@patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_user_included_in_heartbeat(
    mock_amessages: object,
    mock_eval: AsyncMock,
    _mock_hours: MagicMock,
) -> None:
    """User without BOOTSTRAP.md should be eligible for heartbeat after first message."""
    from backend.app.agent.heartbeat import HeartbeatAction, run_heartbeat_for_user

    user = UserData(
        id=31,
        user_id="prepopulated-hb-user",
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
        "Happy to help!"
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        channel="telegram",
    )

    store = get_user_store()
    refreshed = await store.get_by_id(user.id)
    assert refreshed is not None
    assert refreshed.onboarding_complete is True

    # Now verify heartbeat doesn't skip this user
    mock_eval.return_value = HeartbeatAction(
        action_type="no_action",
        message="",
        reasoning="Nothing actionable",
        priority=0,
    )
    result = await run_heartbeat_for_user(
        user=refreshed,
        channel="telegram",
        chat_id=refreshed.channel_identifier,
        max_daily=5,
    )
    # Should get a result (not None which means skipped)
    assert result is not None
    assert result.action_type == "no_action"


# ---------------------------------------------------------------------------
# No completion message tests (finalize is now a no-op)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_no_completion_message_when_already_onboarded(
    mock_amessages: object,
    test_user: UserData,
) -> None:
    """No extra text should be appended for already-onboarded users."""
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
        channel="telegram",
    )

    assert response.reply_text == "Sure, I can help!"
