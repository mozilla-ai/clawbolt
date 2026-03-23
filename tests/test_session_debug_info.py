"""Tests for session debug info: initial_system_prompt and last_compacted_seq."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.agent.core import AgentResponse
from backend.app.agent.dto import StoredMessage
from backend.app.agent.router import PipelineContext, persist_system_prompt_step
from backend.app.agent.session_db import get_session_store
from backend.app.models import ChatSession, Message, User
from tests.conftest import create_test_session


def _create_session_with_prompt(
    user: User,
    session_id: str,
    initial_system_prompt: str = "",
    last_compacted_seq: int = 0,
    messages: list[dict[str, object]] | None = None,
) -> None:
    """Create a session row with optional system prompt and compaction seq."""
    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id=session_id,
            user_id=user.id,
            is_active=True,
            channel="",
            last_compacted_seq=last_compacted_seq,
            initial_system_prompt=initial_system_prompt,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        for msg_data in messages or []:
            ts_str = str(msg_data.get("timestamp", ""))
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(UTC)
            db.add(
                Message(
                    session_id=cs.id,
                    seq=msg_data.get("seq", 1),
                    direction=msg_data.get("direction", "inbound"),
                    body=msg_data.get("body", ""),
                    timestamp=ts,
                )
            )
        db.commit()
    finally:
        db.close()


async def test_system_prompt_stored_on_first_message(test_user: User) -> None:
    """persist_system_prompt_step stores the system prompt on first message."""
    session = create_test_session(test_user.id, "prompt-sess-1")

    msg = session.messages[0] if session.messages else StoredMessage(seq=1)
    ctx = PipelineContext(
        user=test_user,
        session=session,
        message=msg,
        media_urls=[],
        response=AgentResponse(reply_text="Hello!", system_prompt="You are a helpful assistant."),
    )
    await persist_system_prompt_step(ctx)

    # Verify it was persisted
    store = get_session_store(test_user.id)
    loaded = store.load_session("prompt-sess-1")
    assert loaded is not None
    assert loaded.initial_system_prompt == "You are a helpful assistant."


async def test_system_prompt_not_overwritten(test_user: User) -> None:
    """persist_system_prompt_step does not overwrite an existing system prompt."""
    _create_session_with_prompt(
        test_user,
        "prompt-sess-2",
        initial_system_prompt="Original prompt",
    )

    store = get_session_store(test_user.id)
    session = store.load_session("prompt-sess-2")
    assert session is not None

    ctx = PipelineContext(
        user=test_user,
        session=session,
        message=StoredMessage(seq=1),
        media_urls=[],
        response=AgentResponse(reply_text="Hello!", system_prompt="New prompt"),
    )
    await persist_system_prompt_step(ctx)

    loaded = store.load_session("prompt-sess-2")
    assert loaded is not None
    assert loaded.initial_system_prompt == "Original prompt"


def test_api_includes_system_prompt_and_compaction(client: TestClient, test_user: User) -> None:
    """GET /api/user/sessions/{id} includes initial_system_prompt and last_compacted_seq."""
    _create_session_with_prompt(
        test_user,
        "debug-sess-1",
        initial_system_prompt="You are a trades assistant.",
        last_compacted_seq=5,
        messages=[
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
        ],
    )

    resp = client.get("/api/user/sessions/debug-sess-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["initial_system_prompt"] == "You are a trades assistant."
    assert data["last_compacted_seq"] == 5


def test_api_empty_prompt_for_new_session(client: TestClient, test_user: User) -> None:
    """Sessions without a system prompt return empty string."""
    _create_session_with_prompt(
        test_user,
        "debug-sess-2",
        messages=[
            {"direction": "inbound", "body": "Hey", "timestamp": "2025-01-15T10:01:00", "seq": 1},
        ],
    )

    resp = client.get("/api/user/sessions/debug-sess-2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["initial_system_prompt"] == ""
    assert data["last_compacted_seq"] == 0
