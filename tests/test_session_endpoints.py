"""Tests for the user conversation endpoints.

Each user has at most one conversation; these endpoints expose it
without a session_id in the URL, so isolation is enforced by the
auth dependency rather than path validation.
"""

import json
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import ChatSession, Message, User


def _create_session(
    user: User,
    messages: list[dict[str, object]],
    *,
    channel: str = "",
) -> str:
    """Create the user's single ChatSession row with messages.

    Returns the generated session_id (the test client itself never
    needs it; this is only for verification queries).
    """
    db = _db_module.SessionLocal()
    try:
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        cs = ChatSession(
            session_id=session_id,
            user_id=user.id,
            channel=channel,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        for msg_data in messages:
            ts_str = str(msg_data.get("timestamp", ""))
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(UTC)
            db.add(
                Message(
                    session_id=cs.id,
                    seq=msg_data.get("seq", 1),
                    direction=msg_data.get("direction", "inbound"),
                    body=msg_data.get("body", ""),
                    tool_interactions_json=msg_data.get("tool_interactions_json", ""),
                    timestamp=ts,
                )
            )
        db.commit()
        return session_id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/user/conversation
# ---------------------------------------------------------------------------


def test_get_conversation_empty_when_no_session(client: TestClient, test_user: User) -> None:
    """First-time users get an empty shape, not a 404, so the chat UI renders."""
    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == ""
    assert data["messages"] == []


def test_get_conversation_full_detail(client: TestClient, test_user: User) -> None:
    tool_json = json.dumps([{"tool": "save_fact", "input": {"key": "rate"}, "result": "saved"}])
    _create_session(
        test_user,
        [
            {
                "direction": "inbound",
                "body": "Save my rate",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
                "tool_interactions_json": "",
            },
            {
                "direction": "outbound",
                "body": "Done!",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
                "tool_interactions_json": tool_json,
            },
        ],
    )
    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2
    assert data["messages"][0]["tool_interactions"] == []
    assert len(data["messages"][1]["tool_interactions"]) == 1
    assert data["messages"][1]["tool_interactions"][0]["tool"] == "save_fact"


def test_get_conversation_appends_receipts_to_outbound(client: TestClient, test_user: User) -> None:
    """Outbound messages with tool receipts include the rendered receipt block."""
    tool_json = json.dumps(
        [
            {
                "tool_call_id": "call_1",
                "name": "create_companycam_project",
                "args": {},
                "result": "ok",
                "is_error": False,
                "receipt": {
                    "action": "Created CompanyCam project",
                    "target": "Smith Residence",
                    "url": "https://app.companycam.com/projects/12345",
                },
            },
        ]
    )
    _create_session(
        test_user,
        [
            {
                "direction": "inbound",
                "body": "Create a project for Smith",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
            },
            {
                "direction": "outbound",
                "body": "Done!",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
                "tool_interactions_json": tool_json,
            },
        ],
    )
    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    body = resp.json()["messages"][1]["body"]
    assert body.startswith("Done!")
    assert "Created CompanyCam project Smith Residence" in body
    # Compact URL rendering (issue #976) strips the https:// prefix.
    assert "app.companycam.com/projects/12345" in body
    assert "https://" not in body


def test_get_conversation_inbound_body_unchanged(client: TestClient, test_user: User) -> None:
    """Receipt append must only apply to outbound messages, never inbound."""
    tool_json = json.dumps(
        [
            {
                "tool_call_id": "call_1",
                "name": "create_companycam_project",
                "args": {},
                "result": "ok",
                "is_error": False,
                "receipt": {
                    "action": "Created CompanyCam project",
                    "target": "Smith Residence",
                    "url": "https://app.companycam.com/projects/12345",
                },
            },
        ]
    )
    _create_session(
        test_user,
        [
            {
                "direction": "inbound",
                "body": "Create a project for Smith",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
                "tool_interactions_json": tool_json,
            },
            {
                "direction": "outbound",
                "body": "Done!",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
            },
        ],
    )
    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    body = resp.json()["messages"][0]["body"]
    assert body == "Create a project for Smith"


def test_get_conversation_includes_channel(client: TestClient, test_user: User) -> None:
    """Channel field is exposed when present."""
    _create_session(
        test_user,
        [{"direction": "inbound", "body": "Hello", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
        channel="webchat",
    )
    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    assert resp.json()["channel"] == "webchat"


def test_get_conversation_channel_defaults_empty(client: TestClient, test_user: User) -> None:
    """Sessions without channel metadata return empty string."""
    _create_session(
        test_user,
        [{"direction": "inbound", "body": "Hey", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    assert resp.json()["channel"] == ""


def test_get_conversation_scoped_to_authenticated_user(client: TestClient, test_user: User) -> None:
    """A different user's session is invisible to this user's GET."""
    db = _db_module.SessionLocal()
    try:
        other = User(
            id="other-isolation-get",
            user_id="other-iso-get",
            channel_identifier="555100001",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        db.expunge(other)
    finally:
        db.close()

    _create_session(
        other,
        [{"direction": "inbound", "body": "secret", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )

    resp = client.get("/api/user/conversation")
    assert resp.status_code == 200
    # Authenticated as test_user, who has no session of their own.
    assert resp.json()["messages"] == []


# ---------------------------------------------------------------------------
# GET /api/user/conversation/system-prompt
# ---------------------------------------------------------------------------


def test_system_prompt_post_onboarding(client: TestClient, test_user: User) -> None:
    """Post-onboarding users get the regular agent system prompt."""
    _create_session(
        test_user,
        [
            {
                "direction": "inbound",
                "body": "what's my rate?",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
            },
            {
                "direction": "outbound",
                "body": "$95/hr",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
            },
        ],
        channel="webchat",
    )
    resp = client.get("/api/user/conversation/system-prompt")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_onboarding"] is False
    assert "AI assistant for solo tradespeople" in data["system_prompt"]
    # Bootstrap-only phrasing should not leak into a normal-mode prompt.
    assert "first conversation with them" not in data["system_prompt"]


def test_system_prompt_during_onboarding(client: TestClient) -> None:
    """A user still in onboarding gets the bootstrap-flavored prompt."""
    db = _db_module.SessionLocal()
    try:
        user = User(
            id="sp-onboard",
            user_id="onboarding-systemprompt-user",
            channel_identifier="555700001",
            preferred_channel="webchat",
            onboarding_complete=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    from pathlib import Path

    from backend.app.agent.prompts import load_prompt
    from backend.app.config import settings as _settings

    cdir = Path(_settings.data_dir) / "sp-onboard"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "BOOTSTRAP.md").write_text(load_prompt("bootstrap") + "\n", encoding="utf-8")

    from backend.app.auth.dependencies import get_current_user
    from backend.app.main import app as _app

    _app.dependency_overrides[get_current_user] = lambda: user
    try:
        _create_session(
            user,
            [{"direction": "inbound", "body": "hey", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
            channel="webchat",
        )
        resp = client.get("/api/user/conversation/system-prompt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_onboarding"] is True
        assert "first conversation with them" in data["system_prompt"]
    finally:
        _app.dependency_overrides.pop(get_current_user, None)


def test_system_prompt_no_conversation_yet(client: TestClient, test_user: User) -> None:
    """Without a session row, the system-prompt endpoint 404s."""
    resp = client.get("/api/user/conversation/system-prompt")
    assert resp.status_code == 404


def test_system_prompt_empty_messages(client: TestClient, test_user: User) -> None:
    """A session with no messages still returns a valid prompt."""
    _create_session(test_user, [], channel="webchat")
    resp = client.get("/api/user/conversation/system-prompt")
    assert resp.status_code == 200
    assert resp.json()["system_prompt"]


def test_system_prompt_omits_storage_and_outbound_tool_usage_hints(
    client: TestClient, test_user: User
) -> None:
    """Pin the documented preview/runtime divergence for storage/outbound tool hints.

    See ``build_initial_turn_tools``: the preview path passes ``None``
    for the storage and outbound dependencies, so any factory with
    ``requires_storage`` or ``requires_outbound`` is filtered out and
    its per-tool ``usage_hint`` does not appear in the rendered Tool
    Guidelines section.
    """
    _create_session(
        test_user,
        [{"direction": "inbound", "body": "hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
        channel="webchat",
    )
    resp = client.get("/api/user/conversation/system-prompt")
    assert resp.status_code == 200
    prompt = resp.json()["system_prompt"]
    assert "When sending estimates or files, use this to send media" not in prompt
    assert "Upload a recently received file to cloud storage" not in prompt
    assert "Move an unsorted file into the correct client folder" not in prompt


# ---------------------------------------------------------------------------
# DELETE /api/user/conversation/messages/{seq}
# ---------------------------------------------------------------------------


def test_delete_single_message(client: TestClient, test_user: User) -> None:
    """Deleting a single message removes only that message."""
    _create_session(
        test_user,
        [
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {
                "direction": "outbound",
                "body": "Hello!",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
            },
            {"direction": "inbound", "body": "Bye", "timestamp": "2025-01-15T10:03:00", "seq": 3},
        ],
    )
    resp = client.delete("/api/user/conversation/messages/2")
    assert resp.status_code == 200
    assert resp.json()["seq"] == 2

    resp = client.get("/api/user/conversation")
    seqs = [m["seq"] for m in resp.json()["messages"]]
    assert seqs == [1, 3]


def test_delete_single_message_no_conversation(client: TestClient, test_user: User) -> None:
    """Deleting a message before any conversation exists returns 404."""
    resp = client.delete("/api/user/conversation/messages/1")
    assert resp.status_code == 404


def test_delete_single_message_not_found_seq(client: TestClient, test_user: User) -> None:
    """Deleting a nonexistent seq from a valid conversation returns 404."""
    _create_session(
        test_user,
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    resp = client.delete("/api/user/conversation/messages/99")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/user/conversation/messages/batch
# ---------------------------------------------------------------------------


def test_delete_batch_messages(client: TestClient, test_user: User) -> None:
    """Batch deleting specific messages removes only those messages."""
    _create_session(
        test_user,
        [
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {"direction": "outbound", "body": "Hey", "timestamp": "2025-01-15T10:02:00", "seq": 2},
            {"direction": "inbound", "body": "Q", "timestamp": "2025-01-15T10:03:00", "seq": 3},
            {"direction": "outbound", "body": "A", "timestamp": "2025-01-15T10:04:00", "seq": 4},
            {"direction": "inbound", "body": "Bye", "timestamp": "2025-01-15T10:05:00", "seq": 5},
        ],
    )
    resp = client.request(
        "DELETE", "/api/user/conversation/messages/batch", json={"seqs": [2, 3, 4]}
    )
    assert resp.status_code == 200
    assert resp.json()["messages_deleted"] == 3

    resp = client.get("/api/user/conversation")
    seqs = [m["seq"] for m in resp.json()["messages"]]
    assert seqs == [1, 5]


def test_delete_batch_partial(client: TestClient, test_user: User) -> None:
    """Batch delete with some nonexistent seqs deletes only existing ones."""
    _create_session(
        test_user,
        [
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {"direction": "outbound", "body": "Hey", "timestamp": "2025-01-15T10:02:00", "seq": 2},
            {"direction": "inbound", "body": "Bye", "timestamp": "2025-01-15T10:03:00", "seq": 3},
        ],
    )
    resp = client.request(
        "DELETE", "/api/user/conversation/messages/batch", json={"seqs": [1, 2, 99]}
    )
    assert resp.status_code == 200
    assert resp.json()["messages_deleted"] == 2

    resp = client.get("/api/user/conversation")
    seqs = [m["seq"] for m in resp.json()["messages"]]
    assert seqs == [3]


def test_delete_batch_empty_seqs(client: TestClient, test_user: User) -> None:
    """Batch delete with empty seqs returns 422 validation error."""
    _create_session(
        test_user,
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    resp = client.request("DELETE", "/api/user/conversation/messages/batch", json={"seqs": []})
    assert resp.status_code == 422


def test_delete_batch_no_conversation(client: TestClient, test_user: User) -> None:
    """Batch delete before any conversation exists returns 404."""
    resp = client.request("DELETE", "/api/user/conversation/messages/batch", json={"seqs": [1, 2]})
    assert resp.status_code == 404


def test_delete_batch_large(client: TestClient, test_user: User) -> None:
    """Batch delete handles a large number of messages."""
    msgs = [
        {
            "direction": "inbound" if i % 2 == 1 else "outbound",
            "body": f"msg {i}",
            "timestamp": f"2025-01-15T10:{i:02d}:00",
            "seq": i,
        }
        for i in range(1, 51)
    ]
    _create_session(test_user, msgs)
    resp = client.request(
        "DELETE",
        "/api/user/conversation/messages/batch",
        json={"seqs": list(range(1, 51))},
    )
    assert resp.status_code == 200
    assert resp.json()["messages_deleted"] == 50

    resp = client.get("/api/user/conversation")
    assert resp.json()["messages"] == []


# ---------------------------------------------------------------------------
# DELETE /api/user/conversation/messages
# ---------------------------------------------------------------------------


def test_delete_conversation_history(client: TestClient, test_user: User) -> None:
    """Deleting conversation history removes messages but preserves the session."""
    _create_session(
        test_user,
        [
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {
                "direction": "outbound",
                "body": "Hello!",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
            },
        ],
    )
    resp = client.delete("/api/user/conversation/messages")
    assert resp.status_code == 200
    assert resp.json()["messages_deleted"] == 2

    resp = client.get("/api/user/conversation")
    detail = resp.json()
    assert detail["messages"] == []
    assert detail["initial_system_prompt"] == ""


def test_delete_conversation_history_preserves_memory(client: TestClient, test_user: User) -> None:
    """Memory documents are not affected by conversation history deletion."""
    from backend.app.agent.memory_db import get_memory_store

    _create_session(
        test_user,
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    mem_store = get_memory_store(test_user.id)
    mem_store.write_memory("# Test Memory\nImportant fact.")

    resp = client.delete("/api/user/conversation/messages")
    assert resp.status_code == 200

    content = mem_store.read_memory()
    assert "Important fact." in content


def test_delete_conversation_history_no_conversation(client: TestClient, test_user: User) -> None:
    """Delete-all before any conversation exists returns 404."""
    resp = client.delete("/api/user/conversation/messages")
    assert resp.status_code == 404


def test_delete_conversation_history_empty_session(client: TestClient, test_user: User) -> None:
    """Delete-all on an empty session returns 0 deleted, not 404."""
    _create_session(test_user, [])
    resp = client.delete("/api/user/conversation/messages")
    assert resp.status_code == 200
    assert resp.json()["messages_deleted"] == 0


def test_delete_does_not_affect_other_users(client: TestClient, test_user: User) -> None:
    """Authenticated DELETE only touches the caller's conversation."""
    db = _db_module.SessionLocal()
    try:
        other = User(
            id="other-isolation-del",
            user_id="other-iso-del",
            channel_identifier="555100002",
            preferred_channel="telegram",
            onboarding_complete=True,
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        db.expunge(other)
    finally:
        db.close()

    other_session_id = _create_session(
        other,
        [{"direction": "inbound", "body": "secret", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )

    # test_user has no session, so the endpoint 404s without affecting other.
    resp = client.delete("/api/user/conversation/messages")
    assert resp.status_code == 404

    db = _db_module.SessionLocal()
    try:
        cs = db.query(ChatSession).filter_by(session_id=other_session_id).first()
        assert cs is not None
        count = db.query(Message).filter_by(session_id=cs.id).count()
        assert count == 1
    finally:
        db.close()
