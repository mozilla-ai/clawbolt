"""Tests for the ``initial_system_prompt`` capture column.

The column is written by ``persist_system_prompt_step`` for forensics
and intentionally **not** exposed via the public conversation API,
since it reveals the operator preamble and tool wiring. The store
behaviour is exercised below; the API-level negative assertion lives
alongside the conversation endpoint tests.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from backend.app.agent.core import AgentResponse
from backend.app.agent.dto import StoredMessage
from backend.app.agent.router import PipelineContext, persist_system_prompt_step
from backend.app.agent.session_db import get_session_store
from backend.app.database import db_session_async
from backend.app.models import ChatSession, Message, User
from tests.conftest import create_test_session


async def _create_session_with_prompt(
    user: User,
    session_id: str,
    initial_system_prompt: str = "",
    messages: list[dict[str, object]] | None = None,
) -> None:
    """Create a session row with an optional initial system prompt."""
    async with db_session_async() as db:
        cs = ChatSession(
            session_id=session_id,
            user_id=user.id,
            channel="",
            initial_system_prompt=initial_system_prompt,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        await db.flush()
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
        await db.commit()


async def test_system_prompt_stored_on_first_message(test_user: User) -> None:
    """persist_system_prompt_step stores the system prompt on first message."""
    session = await create_test_session(test_user.id, "prompt-sess-1")

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
    loaded = await store.load_session_async("prompt-sess-1")
    assert loaded is not None
    assert loaded.initial_system_prompt == "You are a helpful assistant."


async def test_system_prompt_not_overwritten(test_user: User) -> None:
    """persist_system_prompt_step does not overwrite an existing system prompt."""
    await _create_session_with_prompt(
        test_user,
        "prompt-sess-2",
        initial_system_prompt="Original prompt",
    )

    store = get_session_store(test_user.id)
    session = await store.load_session_async("prompt-sess-2")
    assert session is not None

    ctx = PipelineContext(
        user=test_user,
        session=session,
        message=StoredMessage(seq=1),
        media_urls=[],
        response=AgentResponse(reply_text="Hello!", system_prompt="New prompt"),
    )
    await persist_system_prompt_step(ctx)

    loaded = await store.load_session_async("prompt-sess-2")
    assert loaded is not None
    assert loaded.initial_system_prompt == "Original prompt"


async def test_api_does_not_leak_initial_system_prompt(client: TestClient, test_user: User) -> None:
    """GET /api/user/conversation must not expose the frozen system prompt.

    The capture column stores the operator's preamble and tool wiring at
    the first turn. The dedicated ``/system-prompt`` endpoint is the
    sanctioned read path (premium gates that one behind admin auth). The
    conversation endpoint, which is callable by every authenticated user,
    must not return the field on the wire.
    """
    await _create_session_with_prompt(
        test_user,
        "debug-sess-1",
        initial_system_prompt="You are a trades assistant.",
        messages=[
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
        ],
    )

    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    data = resp.json()
    assert "initial_system_prompt" not in data
    # Belt-and-suspenders: even if the field name is ever re-introduced
    # by accident, make sure the value isn't echoing the stored prompt.
    assert "You are a trades assistant." not in resp.text
