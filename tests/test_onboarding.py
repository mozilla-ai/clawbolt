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
    _has_user_timezone,
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


def test_is_onboarding_needed_self_heals_missing_bootstrap() -> None:
    """Missing BOOTSTRAP.md is self-healed for a user with no profile evidence.

    Regression: previously a missing BOOTSTRAP.md silently returned False,
    so a user whose file was wiped (e.g. OAuth re-login after admin delete,
    ephemeral disk loss) would never get onboarded.
    """
    user = User(id="2", user_id="no-bootstrap-user", phone="+15550002222")
    cdir = Path(settings.data_dir) / str(user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    bootstrap = cdir / "BOOTSTRAP.md"
    assert not bootstrap.exists()

    assert is_onboarding_needed(user) is True
    assert bootstrap.exists()


def test_is_onboarding_needed_no_selfheal_when_heuristic_complete() -> None:
    """Missing BOOTSTRAP.md is NOT re-created when heuristic says onboarded."""
    user = User(
        id="2b",
        user_id="truly-prepopulated",
        phone="+15550002223",
        timezone="America/New_York",
        user_text="# User\n\n- Name: Alice\n- Timezone: America/New_York\n- Trade: GC\n",
        soul_text="# Soul\n\nDirect and practical, no fluff.",
    )
    cdir = Path(settings.data_dir) / str(user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    bootstrap = cdir / "BOOTSTRAP.md"
    assert not bootstrap.exists()

    assert is_onboarding_needed(user) is False
    assert not bootstrap.exists()


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


def test_is_onboarding_needed_bootstrap_deleted_selfheals() -> None:
    """Deleting BOOTSTRAP.md for an un-onboarded user re-creates it (self-heal)."""
    user = User(id="4", user_id="deleted-bootstrap-user", phone="+15550003333")
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True
    _remove_bootstrap(user)
    # Self-heal: no heuristic evidence, so BOOTSTRAP.md is re-written
    assert is_onboarding_needed(user) is True
    assert (Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md").exists()


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
    """User with heuristic evidence of a prior profile gets onboarding_complete=True.

    Covers migrated users whose onboarding_complete flag was never flipped
    but whose user_text already has a real name. The OnboardingSubscriber
    "pre-populated" branch requires heuristic evidence so that users with
    a genuinely empty profile still go through onboarding (via the
    is_onboarding_needed self-heal).
    """
    user_md = "# User\n\n- Name: Alice\n- Timezone: America/New_York\n- Trade: GC\n"
    soul_md = "# Soul\n\nDirect and practical."
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="30",
                user_id="prepopulated-user",
                channel_identifier="888888888",
                preferred_channel="telegram",
                timezone="America/New_York",
                user_text=user_md,
                soul_text=soul_md,
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
        timezone="America/New_York",
        onboarding_complete=False,
        user_text=user_md,
        soul_text=soul_md,
    )
    # No BOOTSTRAP.md; heuristic should say not-needed
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
@patch("backend.app.agent.core.amessages")
async def test_empty_user_without_bootstrap_self_heals_and_onboards(
    mock_amessages: object,
) -> None:
    """An empty user with no BOOTSTRAP.md self-heals and enters onboarding.

    Regression: previously such a user was auto-marked onboarding_complete=True
    (the "pre-populated" branch fired on empty profiles too). This hid the
    real bug where BOOTSTRAP.md had been wiped (e.g. OAuth re-login after
    admin delete) and silently skipped onboarding forever.
    """
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="30b",
                user_id="empty-user",
                channel_identifier="888888889",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="30b",
        user_id="empty-user",
        channel_identifier="888888889",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    # No BOOTSTRAP.md yet, no user_text / soul_text

    session = SessionState(
        session_id="empty-user-session",
        user_id=user.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="hi", seq=1),
        ],
    )
    _ensure_session_on_disk(user, session)
    message = StoredMessage(direction="inbound", body="hi", seq=1)

    mock_amessages.return_value = make_text_response("Hi there!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        channel="telegram",
    )

    # BOOTSTRAP.md should have been re-created by the self-heal
    assert (Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md").exists()

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    # Still onboarding: flag was NOT auto-flipped
    assert refreshed.onboarding_complete is False


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
@patch("backend.app.agent.core.amessages")
async def test_prepopulated_user_included_in_heartbeat(
    mock_amessages: object,
    mock_eval: AsyncMock,
) -> None:
    """User without BOOTSTRAP.md should be eligible for heartbeat after first message."""
    from backend.app.agent.heartbeat import HeartbeatDecision, run_heartbeat_for_user

    user_md = "# User\n\n- Name: Alice\n- Timezone: America/Denver\n- Trade: roofer\n"
    soul_md = "# Soul\n\nFriendly and direct."
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="31",
                user_id="prepopulated-hb-user",
                phone="+15550009999",
                channel_identifier="777777777",
                preferred_channel="telegram",
                timezone="America/Denver",
                heartbeat_text="- Check weather for outdoor jobs",
                user_text=user_md,
                soul_text=soul_md,
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
        timezone="America/Denver",
        onboarding_complete=False,
        user_text=user_md,
        soul_text=soul_md,
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
        _write_user_md(user, "# User\n\n- Name: Alice\n- Trade: GC\n")
        assert _has_real_user_profile(user) is True

    def test_filled_name_flat_format(self) -> None:
        """LLM may rewrite user_text into a flat heading-style format
        without dashes. Both shapes count as evidence of a real profile."""
        user = User(id="104", user_id="flat-name")
        _write_user_md(user, "# User Profile\n\nName: Alice\nBusiness: Acme LLC\n")
        assert _has_real_user_profile(user) is True

    def test_default_template(self) -> None:
        user = User(id="103", user_id="default-template")
        from backend.app.agent.prompts import load_prompt

        _write_user_md(user, f"# User\n\n{load_prompt('default_user')}\n")
        assert _has_real_user_profile(user) is False


class TestHasUserTimezone:
    """Tests for _has_user_timezone heuristic."""

    def test_no_timezone_set(self) -> None:
        user = User(id="105", user_id="no-tz")
        assert _has_user_timezone(user) is False

    def test_empty_timezone(self) -> None:
        user = User(id="106", user_id="empty-tz")
        user.timezone = ""
        assert _has_user_timezone(user) is False

    def test_whitespace_only_timezone(self) -> None:
        user = User(id="108", user_id="whitespace-tz")
        user.timezone = "   "
        assert _has_user_timezone(user) is False

    def test_filled_timezone(self) -> None:
        user = User(id="107", user_id="filled-tz")
        user.timezone = "America/Denver"
        assert _has_user_timezone(user) is True


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
    """Tests for the combined heuristic.

    All three signals must be present (AND, not OR): name, timezone, and
    custom soul. The bootstrap prompt tells the LLM to save the user's
    name as soon as it's heard, so name-only is not evidence that
    onboarding has actually finished. Timezone is one of the two
    strictly-required fields per the prompt.
    """

    def test_no_evidence(self) -> None:
        user = User(id="120", user_id="no-evidence")
        assert is_onboarding_complete_heuristic(user) is False

    def test_name_only_is_not_enough(self) -> None:
        user = User(id="121", user_id="name-only")
        _write_user_md(user, "# User\n\n- Name: Jake\n")
        assert is_onboarding_complete_heuristic(user) is False

    def test_soul_only_is_not_enough(self) -> None:
        user = User(id="122", user_id="soul-only")
        _write_soul_md(user, "# Soul\n\nCustom personality.")
        assert is_onboarding_complete_heuristic(user) is False

    def test_name_and_soul_without_timezone_is_not_enough(self) -> None:
        user = User(id="123", user_id="name-and-soul")
        _write_user_md(user, "# User\n\n- Name: Alice\n")
        _write_soul_md(user, "# Soul\n\nCustom personality.")
        assert is_onboarding_complete_heuristic(user) is False

    def test_name_timezone_and_soul(self) -> None:
        user = User(id="124", user_id="all-three")
        _write_user_md(user, "# User\n\n- Name: Alice\n- Trade: roofer\n")
        user.timezone = "America/New_York"
        _write_soul_md(user, "# Soul\n\nCustom personality.")
        assert is_onboarding_complete_heuristic(user) is True

    def test_flat_format_user_text_with_db_timezone(self) -> None:
        """The combined heuristic must pass for the real-world case: LLM
        writes user_text in flat format and the timezone column is
        populated by the dedicated TZ tool. Regression test for the bug
        that left first-user stuck on 'pending' despite a complete profile."""
        user = User(id="125", user_id="flat-with-db-tz")
        _write_user_md(
            user,
            "# User Profile\n\nName: Alice\nBusiness: Acme LLC\nTrade: GC\n",
        )
        user.timezone = "America/New_York"
        _write_soul_md(user, "# Soul\n\nCustom personality.")
        assert is_onboarding_complete_heuristic(user) is True


def test_is_onboarding_needed_heuristic_override() -> None:
    """BOOTSTRAP.md exists but heuristic says onboarding is done."""
    user = User(id="130", user_id="heuristic-user")
    _create_bootstrap(user)
    _write_user_md(
        user,
        "# User\n\n- Name: Alice\n- Trade: GC\n",
    )
    user.timezone = "America/New_York"
    _write_soul_md(user, "# Soul\n\nDirect and practical.")
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
    """Onboarding completes via heuristic even if LLM never deletes BOOTSTRAP.md.

    Regression test for #639: if the LLM gets sidetracked (e.g. user asks
    a real question) and never calls delete_file("BOOTSTRAP.md"), the
    heuristic fallback marks onboarding complete once all gates pass:
    name + timezone + custom soul are present AND the user has sent at
    least MIN_ONBOARDING_USER_MESSAGES messages.
    """
    db = _db_module.SessionLocal()
    try:
        # Timezone is set via the dashboard / browser PUT /user/profile flow,
        # not by an in-conversation tool. Pre-populate it so the heuristic
        # has the timezone signal it expects from the DB column.
        db.add(
            User(
                id="140",
                user_id="sidetracked-user",
                channel_identifier="555555555",
                preferred_channel="telegram",
                timezone="America/New_York",
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
        timezone="America/New_York",
        onboarding_complete=False,
    )
    _create_bootstrap(user)
    assert is_onboarding_needed(user) is True

    # Simulate: the LLM writes USER.md (with name + timezone) and SOUL.md
    # but does NOT delete BOOTSTRAP.md.
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": (
                            "# User\n\n- Name: Nathan\n- Timezone: America/New_York\n- Trade: GC\n"
                        ),
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

    # The user message-count gate requires at least 10 inbound messages.
    # Build a realistic onboarding lead-in plus the current turn.
    prior_messages: list[StoredMessage] = []
    for i in range(1, 19, 2):  # 9 inbound, 9 outbound
        prior_messages.append(StoredMessage(direction="inbound", body=f"u{i}", seq=i))
        prior_messages.append(StoredMessage(direction="outbound", body=f"a{i}", seq=i + 1))
    current_message = StoredMessage(direction="inbound", body="I'm Nathan, a GC", seq=19)
    session = SessionState(
        session_id="onboard-session",
        user_id=user.id,
        is_active=True,
        messages=[*prior_messages, current_message],
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
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


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_heuristic_does_not_fire_when_only_name_set_early(
    mock_amessages: object,
) -> None:
    """Regression: name saved on turn 2 alone must NOT mark onboarding complete.

    The bootstrap prompt instructs the LLM to save the user's name as
    soon as it's heard. Earlier the heuristic used name OR custom soul,
    so the moment write_file landed a name on turn 2-3, onboarding got
    cut off before timezone, trade context, and personality were
    collected. With AND-logic + the user-message gate, this turn must
    leave the user still in onboarding.
    """
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="141",
                user_id="early-name-user",
                channel_identifier="555555556",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="141",
        user_id="early-name-user",
        channel_identifier="555555556",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    _create_bootstrap(user)

    # LLM writes only the name to USER.md (no timezone, no custom soul).
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": "# User\n\n- Name: Jesse\n",
                    }
                ),
            },
        ]
    )
    text_response = make_text_response("Saved that. What kind of work do you do?")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    current_message = StoredMessage(direction="inbound", body="i'm jesse", seq=3)
    session = SessionState(
        session_id="early-session",
        user_id=user.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="hey", seq=1),
            StoredMessage(direction="outbound", body="hi I'm Clawbolt", seq=2),
            current_message,
        ],
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
        media_urls=[],
        channel="telegram",
    )

    # Both gates must keep us in onboarding: only 2 user messages, and
    # neither timezone nor custom soul was written.
    bootstrap = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    assert bootstrap.exists()

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is False


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_heuristic_blocked_by_message_count_gate(
    mock_amessages: object,
) -> None:
    """All content gates pass but message count is below the floor.

    Even if the LLM somehow writes a complete name + timezone + custom
    soul on turn 2, the user-message-count gate keeps the user in
    onboarding until at least MIN_ONBOARDING_USER_MESSAGES turns have
    happened. This is the second layer of defense.
    """
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="142",
                user_id="fast-writer-user",
                channel_identifier="555555557",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="142",
        user_id="fast-writer-user",
        channel_identifier="555555557",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    _create_bootstrap(user)

    # LLM lands all three signals on the very first turn (unrealistic but
    # protects against a future prompt change that frontloads everything).
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": ("# User\n\n- Name: Pat\n- Timezone: America/Los_Angeles\n"),
                    }
                ),
            },
            {
                "id": "call_soul",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "SOUL.md",
                        "content": "# Soul\n\nDirect.",
                    }
                ),
            },
        ]
    )
    text_response = make_text_response("Got it.")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    current_message = StoredMessage(direction="inbound", body="hey", seq=1)
    session = SessionState(
        session_id="fast-session",
        user_id=user.id,
        is_active=True,
        messages=[current_message],
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
        media_urls=[],
        channel="telegram",
    )

    bootstrap = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    assert bootstrap.exists()

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is False


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_onboarding_force_completes_at_max_user_messages(
    mock_amessages: object,
) -> None:
    """At MAX_ONBOARDING_USER_MESSAGES the user is force-completed.

    Last-resort safety net: even if the LLM never satisfies any content
    signal (no name, no timezone, no custom soul, no BOOTSTRAP.md
    deletion), at the hard ceiling we flip the flag so heartbeats and
    other gated features don't stay disabled indefinitely.
    """
    from backend.app.agent.onboarding import MAX_ONBOARDING_USER_MESSAGES

    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="143",
                user_id="endless-onboarding-user",
                channel_identifier="555555558",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="143",
        user_id="endless-onboarding-user",
        channel_identifier="555555558",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    _create_bootstrap(user)

    # LLM does nothing useful: no tool calls, just chats back.
    mock_amessages.return_value = make_text_response(  # type: ignore[union-attr]
        "Tell me more."
    )

    # Build a session with MAX_ONBOARDING_USER_MESSAGES inbound messages
    # (current message included in that count).
    prior_messages: list[StoredMessage] = []
    inbound_target = MAX_ONBOARDING_USER_MESSAGES - 1
    seq = 1
    for _ in range(inbound_target):
        prior_messages.append(StoredMessage(direction="inbound", body="hi", seq=seq))
        seq += 1
        prior_messages.append(StoredMessage(direction="outbound", body="ok", seq=seq))
        seq += 1
    current_message = StoredMessage(direction="inbound", body="still here", seq=seq)
    session = SessionState(
        session_id="endless-session",
        user_id=user.id,
        is_active=True,
        messages=[*prior_messages, current_message],
    )
    _ensure_session_on_disk(user, session)

    # Sanity: profile text remains empty so the heuristic gate would NOT
    # fire on its own. The force-completion path is what drives this.
    assert not user.user_text
    assert not is_onboarding_complete_heuristic(user)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
        media_urls=[],
        channel="telegram",
    )

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


# ---------------------------------------------------------------------------
# Auto-exit path (post-2026-04 bootstrap): system removes BOOTSTRAP.md
# once name + timezone are captured AND the conversation has texture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_auto_exit_when_name_tz_captured_and_min_turns_reached(
    mock_amessages: object,
) -> None:
    """The system removes BOOTSTRAP.md once name + tz are captured AND the
    user has sent at least MIN_USER_MESSAGES_FOR_AUTO_EXIT messages.

    The LLM does NOT need to call delete_file. This is the primary
    completion path in the post-2026-04 bootstrap design: the LLM
    has no exit-decision burden, and bootstrap-only guidance
    disappears from the system prompt automatically.
    """
    from backend.app.agent.onboarding import MIN_USER_MESSAGES_FOR_AUTO_EXIT

    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="201",
                user_id="auto-exit-user",
                channel_identifier="555200001",
                preferred_channel="telegram",
                timezone="America/Chicago",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="201",
        user_id="auto-exit-user",
        channel_identifier="555200001",
        preferred_channel="telegram",
        timezone="America/Chicago",
        onboarding_complete=False,
    )
    _create_bootstrap(user)

    # LLM saves name + timezone to USER.md (no soul customization needed
    # under the new design).
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": ("# User\n\n- Name: Jordan\n- Timezone: America/Chicago\n"),
                    }
                ),
            }
        ]
    )
    text_response = make_text_response("Got it.")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    # Build a session with exactly MIN_USER_MESSAGES_FOR_AUTO_EXIT inbound
    # messages, current message included.
    prior_messages: list[StoredMessage] = []
    seq = 1
    for _ in range(MIN_USER_MESSAGES_FOR_AUTO_EXIT - 1):
        prior_messages.append(StoredMessage(direction="inbound", body="u", seq=seq))
        seq += 1
        prior_messages.append(StoredMessage(direction="outbound", body="a", seq=seq))
        seq += 1
    current_message = StoredMessage(direction="inbound", body="I'm Jordan in Chicago", seq=seq)
    session = SessionState(
        session_id="auto-exit-session",
        user_id=user.id,
        is_active=True,
        messages=[*prior_messages, current_message],
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
        media_urls=[],
        channel="telegram",
    )

    bootstrap = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    assert not bootstrap.exists(), "auto-exit should remove BOOTSTRAP.md"

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
@patch("backend.app.agent.core.amessages")
async def test_auto_exit_does_not_fire_below_min_turns(
    mock_amessages: object,
) -> None:
    """Even with name + timezone captured, auto-exit waits for the message
    floor so the conversation has texture beyond data capture."""
    db = _db_module.SessionLocal()
    try:
        db.add(
            User(
                id="202",
                user_id="too-fast-user",
                channel_identifier="555200002",
                preferred_channel="telegram",
                timezone="America/Chicago",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="202",
        user_id="too-fast-user",
        channel_identifier="555200002",
        preferred_channel="telegram",
        timezone="America/Chicago",
        onboarding_complete=False,
    )
    _create_bootstrap(user)

    # LLM lands name + tz on the very first inbound.
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": "# User\n\n- Name: Sam\n- Timezone: America/Chicago\n",
                    }
                ),
            }
        ]
    )
    text_response = make_text_response("Got it.")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    current_message = StoredMessage(direction="inbound", body="I'm Sam in Chicago", seq=1)
    session = SessionState(
        session_id="too-fast-session",
        user_id=user.id,
        is_active=True,
        messages=[current_message],
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
        media_urls=[],
        channel="telegram",
    )

    bootstrap = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    assert bootstrap.exists(), "auto-exit should NOT fire on turn 1"

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is False


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_auto_exit_does_not_fire_without_timezone(
    mock_amessages: object,
) -> None:
    """Auto-exit requires both name AND timezone. Name alone is not enough."""
    from backend.app.agent.onboarding import MIN_USER_MESSAGES_FOR_AUTO_EXIT

    db = _db_module.SessionLocal()
    try:
        # Note: no timezone set on the user row.
        db.add(
            User(
                id="203",
                user_id="no-tz-user",
                channel_identifier="555200003",
                preferred_channel="telegram",
            )
        )
        db.commit()
    finally:
        db.close()

    user = User(
        id="203",
        user_id="no-tz-user",
        channel_identifier="555200003",
        preferred_channel="telegram",
        onboarding_complete=False,
    )
    _create_bootstrap(user)

    # LLM saves name only (no timezone).
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_user",
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "path": "USER.md",
                        "content": "# User\n\n- Name: Casey\n",
                    }
                ),
            }
        ]
    )
    text_response = make_text_response("Got it, where are you based?")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    prior_messages: list[StoredMessage] = []
    seq = 1
    for _ in range(MIN_USER_MESSAGES_FOR_AUTO_EXIT - 1):
        prior_messages.append(StoredMessage(direction="inbound", body="u", seq=seq))
        seq += 1
        prior_messages.append(StoredMessage(direction="outbound", body="a", seq=seq))
        seq += 1
    current_message = StoredMessage(direction="inbound", body="I'm Casey", seq=seq)
    session = SessionState(
        session_id="no-tz-session",
        user_id=user.id,
        is_active=True,
        messages=[*prior_messages, current_message],
    )
    _ensure_session_on_disk(user, session)

    await handle_inbound_message(
        user=user,
        session=session,
        message=current_message,
        media_urls=[],
        channel="telegram",
    )

    bootstrap = Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"
    assert bootstrap.exists(), "auto-exit must NOT fire without timezone"

    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=user.id).first()
        if refreshed:
            db.expunge(refreshed)
    finally:
        db.close()
    assert refreshed is not None
    assert refreshed.onboarding_complete is False


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
