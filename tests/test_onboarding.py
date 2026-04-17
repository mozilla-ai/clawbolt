import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import backend.app.database as _db_module
from backend.app.agent.file_store import (
    SessionState,
    StoredMessage,
)
from backend.app.agent.onboarding import (
    _has_custom_soul,
    _has_real_user_profile,
    build_onboarding_system_prompt,
    is_onboarding_complete_heuristic,
    is_onboarding_needed,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.models import User
from tests.mocks.llm import extract_system_text, make_text_response, make_tool_call_response


def _ensure_session_on_disk(user: User, session: SessionState) -> None:
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
        data = {c.key: getattr(user, c.key) for c in user.__table__.columns if c.key != "soul_text"}
        user_json.write_text(json.dumps(data, default=str), encoding="utf-8")


def _create_bootstrap(user: User) -> None:
    """Create a BOOTSTRAP.md file for the given user from the real template."""
    from backend.app.agent.prompts import load_prompt

    cdir = Path(settings.data_dir) / str(user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "BOOTSTRAP.md").write_text(load_prompt("bootstrap") + "\n", encoding="utf-8")


def _remove_bootstrap(user: User) -> None:
    """Remove BOOTSTRAP.md for the given user."""
    path = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    if path.exists():
        path.unlink()


def test_is_onboarding_needed_new_user() -> None:
    """New user with BOOTSTRAP.md should need onboarding."""
    user = User(id="1", user_id="new-user", phone="+15550001111")
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True


def test_is_onboarding_needed_no_bootstrap() -> None:
    """User without BOOTSTRAP.md should not need onboarding."""
    user = User(id="2", user_id="no-bootstrap-user", phone="+15550002222")
    # Ensure user dir exists but no BOOTSTRAP.md
    cdir = Path(settings.data_dir) / str(user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    assert is_onboarding_needed(user) is False


def test_is_onboarding_needed_complete_profile(test_user: User) -> None:
    """User with onboarding_complete=True does not need onboarding."""
    assert is_onboarding_needed(test_user) is False


def test_is_onboarding_needed_respects_flag() -> None:
    """User with onboarding_complete=True should not need onboarding even with BOOTSTRAP.md."""
    user = User(
        id="3",
        user_id="flagged-user",
        phone="+15550007777",
        onboarding_complete=True,
    )
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is False


def test_provision_user_creates_bootstrap_and_seeds_db() -> None:
    """provision_user should seed DB text columns and create BOOTSTRAP.md."""
    from backend.app.agent.user_db import provision_user
    from backend.app.database import SessionLocal

    db = SessionLocal()
    try:
        user = User(id="provision-test", user_id="provision-user")
        db.add(user)
        db.commit()
        db.refresh(user)

        provision_user(user, db)

        # DB columns should be seeded (except heartbeat, which waits for onboarding)
        db.refresh(user)
        assert user.soul_text
        assert user.user_text
        assert not user.heartbeat_text

        # BOOTSTRAP.md on disk
        user_dir = Path(settings.data_dir) / str(user.id)
        assert (user_dir / "BOOTSTRAP.md").exists()
        assert is_onboarding_needed(user) is True
    finally:
        db.close()


def test_provision_skips_bootstrap_when_onboarding_complete() -> None:
    """provision_user should not create BOOTSTRAP.md for onboarded users."""
    from backend.app.agent.user_db import provision_user
    from backend.app.database import SessionLocal

    db = SessionLocal()
    try:
        user = User(id="provision-complete", user_id="done-user", onboarding_complete=True)
        db.add(user)
        db.commit()
        db.refresh(user)

        provision_user(user, db)

        user_dir = Path(settings.data_dir) / str(user.id)
        assert not (user_dir / "BOOTSTRAP.md").exists()
        assert is_onboarding_needed(user) is False
    finally:
        db.close()


def test_is_onboarding_needed_bootstrap_deleted() -> None:
    """After BOOTSTRAP.md is deleted, onboarding is not needed."""
    user = User(id="4", user_id="deleted-bootstrap-user", phone="+15550003333")
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True
    _remove_bootstrap(user)
    assert is_onboarding_needed(user) is False


def test_build_onboarding_system_prompt_new_user() -> None:
    """Onboarding prompt for new user should include bootstrap content."""
    user = User(id="5", user_id="brand-new", phone="+15550004444")
    _create_bootstrap(user)

    prompt = build_onboarding_system_prompt(user)
    assert "help them with that request FIRST" in prompt


def test_build_onboarding_system_prompt_includes_dictation_tip() -> None:
    """Onboarding system prompt should include the phone dictation tip."""
    user = User(id="6b", user_id="dictation-user", phone="+15550006666")
    _create_bootstrap(user)

    prompt = build_onboarding_system_prompt(user)
    assert "dictation" in prompt.lower()
    assert "microphone" in prompt.lower()


def test_build_onboarding_system_prompt_includes_tool_capabilities() -> None:
    """Onboarding system prompt should inject available specialist tool descriptions."""
    user = User(id="6", user_id="new-user", phone="+15550001111")
    _create_bootstrap(user)

    prompt = build_onboarding_system_prompt(user)
    # Should include specialist tool summaries from the registry
    assert "specialist capabilities" in prompt.lower()
    assert "estimate" in prompt.lower()


def test_build_onboarding_system_prompt_includes_instructions() -> None:
    """Onboarding prompt should include behavioral instructions and communication guidance.

    Regression: the old onboarding prompt replaced the entire system prompt,
    stripping away tool guidelines. The model didn't know to reply directly
    with text and returned empty responses.
    """
    from pydantic import BaseModel

    from backend.app.agent.tools.base import Tool, ToolResult

    user = User(id="7", user_id="instructions-test", phone="+15550005555")
    _create_bootstrap(user)

    class _SendMediaParams(BaseModel):
        message: str
        media_url: str

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tools = [
        Tool(
            name="send_media_reply",
            description="Send a reply with a media attachment.",
            function=dummy,
            params_model=_SendMediaParams,
            usage_hint="When sending estimates or files, use this to send media.",
        ),
    ]
    prompt = build_onboarding_system_prompt(user, tools=tools)
    # Should include the communication instruction from instructions.md
    assert "Reply directly with text" in prompt
    # Should include tool usage hint
    assert "media" in prompt.lower()


# --- Fixtures ---


@pytest.fixture()
def new_user() -> User:
    """User with no profile, needs onboarding."""
    import backend.app.database as _db_module

    # Create in DB so onboarding subscriber can find it
    db = _db_module.SessionLocal()
    try:
        db_user = User(
            id="20",
            user_id="new-user-onboard",
            phone="+15559999999",
            channel_identifier="999999999",
        )
        db.add(db_user)
        db.commit()
    finally:
        db.close()

    user = User(
        id="20",
        user_id="new-user-onboard",
        phone="+15559999999",
        channel_identifier="999999999",
    )
    _create_bootstrap(user)
    return user


@pytest.fixture()
def onboarding_session(new_user: User) -> SessionState:
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
    new_user: User,
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
    system_msg = extract_system_text(call_args.kwargs["system"])
    # bootstrap.md content anchors the onboarding prompt; check for a line
    # that's specific to it and unlikely to appear in the regular prompt.
    assert "first conversation" in system_msg or "blank slate" in system_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_completes_when_bootstrap_deleted(
    mock_amessages: object,
    new_user: User,
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

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=new_user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is True
    # Heartbeat items remain empty; users add them as needed
    assert not refreshed.heartbeat_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_complete_profile_uses_normal_prompt(
    mock_amessages: object,
    test_user: User,
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
    system_msg = extract_system_text(call_args.kwargs["system"])
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
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="30",
                user_id="prepopulated-user",
                channel_identifier="888888888",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="30",
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

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is True


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_user_included_in_heartbeat(
    mock_amessages: object,
    mock_eval: AsyncMock,
) -> None:
    """User without BOOTSTRAP.md should be eligible for heartbeat after first message."""
    from backend.app.agent.heartbeat import HeartbeatDecision, run_heartbeat_for_user

    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="31",
                user_id="prepopulated-hb-user",
                phone="+15550009999",
                channel_identifier="777777777",
                preferred_channel="telegram",
                heartbeat_text="- Check weather for outdoor jobs",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="31",
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

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is True

    # Now verify heartbeat doesn't skip this user
    mock_eval.return_value = HeartbeatDecision(
        action="skip",
        tasks="",
        reasoning="Nothing actionable",
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
    test_user: User,
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


# ---------------------------------------------------------------------------
# Heuristic onboarding completion tests (#639)
# ---------------------------------------------------------------------------


def _write_user_md(user: User, content: str) -> None:
    """Set user_text on the User object (was: write USER.md to disk)."""
    user.user_text = content


def _write_soul_md(user: User, content: str) -> None:
    """Set soul_text on the User object (was: write SOUL.md to disk)."""
    user.soul_text = content


class TestHasRealUserProfile:
    """Tests for _has_real_user_profile heuristic."""

    def test_no_user_md(self) -> None:
        user = User(id="100", user_id="no-user-md")
        assert _has_real_user_profile(user) is False

    def test_empty_name_field(self) -> None:
        user = User(id="101", user_id="empty-name")
        _write_user_md(user, "# User\n\n- Name:\n- Timezone:\n")
        assert _has_real_user_profile(user) is False

    def test_filled_name_field(self) -> None:
        user = User(id="102", user_id="filled-name")
        _write_user_md(user, "# User\n\n- Name: Nathan\n- Trade: GC\n")
        assert _has_real_user_profile(user) is True

    def test_default_template(self) -> None:
        user = User(id="103", user_id="default-template")
        from backend.app.agent.prompts import load_prompt

        _write_user_md(user, f"# User\n\n{load_prompt('default_user')}\n")
        assert _has_real_user_profile(user) is False


class TestHasCustomSoul:
    """Tests for _has_custom_soul heuristic."""

    def test_no_soul_md(self) -> None:
        user = User(id="110", user_id="no-soul")
        assert _has_custom_soul(user) is False

    def test_default_soul(self) -> None:
        user = User(id="111", user_id="default-soul")
        from backend.app.agent.prompts import load_prompt

        _write_soul_md(user, load_prompt("default_soul"))
        assert _has_custom_soul(user) is False

    def test_default_soul_wrapped(self) -> None:
        """Default soul written by _ensure_user_dir includes a '# Soul' header."""
        user = User(id="113", user_id="default-soul-wrapped")
        from backend.app.agent.prompts import load_prompt

        _write_soul_md(user, f"# Soul\n\n{load_prompt('default_soul')}")
        assert _has_custom_soul(user) is False

    def test_custom_soul(self) -> None:
        user = User(id="112", user_id="custom-soul")
        _write_soul_md(user, "# Soul\n\nI'm Clawbolt. Straight and to the point.")
        assert _has_custom_soul(user) is True


class TestIsOnboardingCompleteHeuristic:
    """Tests for the combined heuristic."""

    def test_no_evidence(self) -> None:
        user = User(id="120", user_id="no-evidence")
        assert is_onboarding_complete_heuristic(user) is False

    def test_name_only(self) -> None:
        user = User(id="121", user_id="name-only")
        _write_user_md(user, "# User\n\n- Name: Jake\n")
        assert is_onboarding_complete_heuristic(user) is True

    def test_soul_only(self) -> None:
        user = User(id="122", user_id="soul-only")
        _write_soul_md(user, "# Soul\n\nCustom personality.")
        assert is_onboarding_complete_heuristic(user) is True


def test_is_onboarding_needed_heuristic_override() -> None:
    """BOOTSTRAP.md exists but heuristic says onboarding is done."""
    user = User(id="130", user_id="heuristic-user")
    _create_bootstrap(user)
    _write_user_md(user, "# User\n\n- Name: Nathan\n- Trade: GC\n")
    # BOOTSTRAP.md exists, but heuristic detects completed onboarding
    assert is_onboarding_needed(user) is False


def test_is_onboarding_needed_no_heuristic_evidence() -> None:
    """BOOTSTRAP.md exists and no heuristic evidence: still needs onboarding."""
    user = User(id="131", user_id="fresh-user")
    _create_bootstrap(user)
    # No USER.md or SOUL.md written yet
    assert is_onboarding_needed(user) is True


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_completes_via_heuristic_when_bootstrap_not_deleted(
    mock_amessages: object,
) -> None:
    """Onboarding should complete via heuristic even if LLM never deletes BOOTSTRAP.md.

    Regression test for #639: if the LLM gets sidetracked (e.g. user asks
    a real question) and never calls delete_file("BOOTSTRAP.md"), the
    heuristic fallback should detect that USER.md has a real name and mark
    onboarding complete.
    """
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="140",
                user_id="sidetracked-user",
                channel_identifier="555555555",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="140",
        user_id="sidetracked-user",
        channel_identifier="555555555",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True

    # Simulate: the LLM writes USER.md and SOUL.md but does NOT delete BOOTSTRAP.md
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": "# User\n\n- Name: Nathan\n- Trade: GC\n",
                    }
                ),
            },
            {
                "id": "call_soul",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "SOUL.md",
                        "content": "# Soul\n\nStraight and to the point.",
                    }
                ),
            },
        ]
    )
    text_response = make_text_response("Got it Nathan! What invoices do you need?")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    session = SessionState(
        session_id="onboard-session",
        user_id=user.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="I'm Nathan, a GC", seq=1),
        ],
    )
    _ensure_session_on_disk(user, session)
    message = StoredMessage(direction="inbound", body="I'm Nathan, a GC", seq=1)

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        channel="telegram",
    )

    # BOOTSTRAP.md should have been cleaned up by the heuristic
    bootstrap = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    assert not bootstrap.exists()

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is True
    # Heartbeat items remain empty; users add them as needed
    assert not refreshed.heartbeat_text


# ---------------------------------------------------------------------------
# Regression test: premium OAuth users must be provisioned on first chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_oauth_user_provisioned_on_first_chat() -> None:
    """User created via OAuth (no provision_user call) should be provisioned on first chat.

    Regression test: premium creates User rows during Google OAuth signup
    via UserStore.create(), which does NOT call provision_user(). When the
    user then sends their first webchat message, _get_or_create_user()
    found the existing user by PK but returned it without provisioning.
    Result: no BOOTSTRAP.md, no soul_text/user_text, onboarding never triggered.
    """
    from backend.app.agent.ingestion import _get_or_create_user
    from backend.app.agent.onboarding import is_onboarding_needed
    from backend.app.config import settings as app_settings

    # Simulate OAuth signup: create a bare User row (no provision_user call)
    db = _db_module.SessionLocal()
    try:
        user = User(
            id="oauth-premium-user",
            user_id="google_12345",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        # Confirm: no soul_text, no user_text, no BOOTSTRAP.md
        assert not user.soul_text
        assert not user.user_text
        assert not user.onboarding_complete
    finally:
        db.close()

    bootstrap_path = Path(app_settings.data_dir) / "oauth-premium-user" / "BOOTSTRAP.md"
    assert not bootstrap_path.exists()

    # Simulate premium webchat: sender_id = user.id (the PK)
    # Enable premium_plugin so the single-tenant reuse path is skipped
    with patch.object(app_settings, "premium_plugin", "clawbolt_premium.plugin"):
        resolved = await _get_or_create_user("webchat", "oauth-premium-user")

    # User should now be provisioned
    assert resolved.id == "oauth-premium-user"
    assert resolved.soul_text  # seeded by provision_user
    assert resolved.user_text  # seeded by provision_user
    assert bootstrap_path.exists()  # created by provision_user
    assert is_onboarding_needed(resolved) is True


@pytest.mark.asyncio()
async def test_preferred_channel_updates_on_channel_switch() -> None:
    """preferred_channel should update when a returning user messages from a different channel.

    Regression: heartbeat used preferred_channel to pick the delivery channel,
    but preferred_channel was never updated in premium mode when the user
    switched from Telegram to iMessage (linq). Heartbeats kept going to the
    old channel.
    """
    from backend.app.agent.ingestion import _get_or_create_user
    from backend.app.models import ChannelRoute

    # Create a user who signed up via Telegram
    db = _db_module.SessionLocal()
    try:
        user = User(
            id="channel-switch-user",
            user_id="google_switch",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(user)
        db.flush()
        db.add(ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="tg_123"))
        db.add(ChannelRoute(user_id=user.id, channel="linq", channel_identifier="linq_456"))
        db.commit()
    finally:
        db.close()

    # User sends a message via linq (iMessage)
    resolved = await _get_or_create_user("linq", "linq_456")

    assert resolved.id == "channel-switch-user"
    assert resolved.preferred_channel == "linq"
