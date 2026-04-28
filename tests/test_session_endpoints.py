"""Tests for conversation session endpoints."""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import ChatSession, Message, User


def _create_session(
    user: User,
    session_id: str,
    messages: list[dict[str, object]],
    channel: str = "",
) -> None:
    """Create a session with messages in the database."""
    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id=session_id,
            user_id=user.id,
            is_active=True,
            channel=channel,
            last_compacted_seq=0,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        for msg_data in messages:
            ts_str = str(msg_data.get("timestamp", ""))
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(UTC)
            msg = Message(
                session_id=cs.id,
                seq=msg_data.get("seq", 1),
                direction=msg_data.get("direction", "inbound"),
                body=msg_data.get("body", ""),
                tool_interactions_json=msg_data.get("tool_interactions_json", ""),
                timestamp=ts,
            )
            db.add(msg)
        db.commit()
    finally:
        db.close()


def test_get_session_detail(client: TestClient, test_user: User) -> None:
    tool_json = json.dumps([{"tool": "save_fact", "input": {"key": "rate"}, "result": "saved"}])
    _create_session(
        test_user,
        "1_200",
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
    resp = client.get("/api/user/sessions/1_200")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "1_200"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["tool_interactions"] == []
    assert len(data["messages"][1]["tool_interactions"]) == 1
    assert data["messages"][1]["tool_interactions"][0]["tool"] == "save_fact"


def test_get_session_detail_appends_receipts_to_outbound(
    client: TestClient, test_user: User
) -> None:
    """Outbound messages with tool receipts should include the rendered
    receipt block in the returned body, matching what iMessage/Telegram
    users see."""
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
        "1_250",
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
    resp = client.get("/api/user/sessions/1_250")
    assert resp.status_code == 200
    body = resp.json()["messages"][1]["body"]
    assert body.startswith("Done!")
    assert "Created CompanyCam project Smith Residence" in body
    # Compact URL rendering (issue #976) strips the https:// prefix.
    assert "app.companycam.com/projects/12345" in body
    assert "https://" not in body


def test_get_session_detail_inbound_body_unchanged(client: TestClient, test_user: User) -> None:
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
        "1_260",
        [
            {
                "direction": "inbound",
                "body": "Hello",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
                "tool_interactions_json": tool_json,
            },
        ],
    )
    resp = client.get("/api/user/sessions/1_260")
    assert resp.status_code == 200
    assert resp.json()["messages"][0]["body"] == "Hello"


def test_session_direction_values(client: TestClient, test_user: User) -> None:
    """API response direction values must be 'inbound'/'outbound' (not 'incoming'/'outgoing')."""
    _create_session(
        test_user,
        "1_300",
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
    resp = client.get("/api/user/sessions/1_300")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"][0]["direction"] == "inbound"
    assert data["messages"][1]["direction"] == "outbound"


def test_get_session_not_found(client: TestClient) -> None:
    resp = client.get("/api/user/sessions/nonexistent")
    assert resp.status_code == 404


def test_session_detail_includes_channel(client: TestClient, test_user: User) -> None:
    """Session detail should include the channel field when present in metadata."""
    _create_session(
        test_user,
        "1_500",
        [{"direction": "inbound", "body": "Hello", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
        channel="webchat",
    )
    resp = client.get("/api/user/sessions/1_500")
    assert resp.status_code == 200
    data = resp.json()
    assert data["channel"] == "webchat"


def test_session_channel_defaults_empty(client: TestClient, test_user: User) -> None:
    """Sessions without channel metadata should return an empty string."""
    _create_session(
        test_user,
        "1_600",
        [{"direction": "inbound", "body": "Hey", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    resp = client.get("/api/user/sessions/1_600")
    assert resp.status_code == 200
    data = resp.json()
    assert data["channel"] == ""


# ---------------------------------------------------------------------------
# GET /api/user/sessions/{session_id}/system-prompt
# ---------------------------------------------------------------------------


def test_get_system_prompt_returns_live_post_onboarding(
    client: TestClient, test_user: User
) -> None:
    """Post-onboarding users get the regular agent system prompt.

    The fixture's ``test_user`` is onboarded, so the endpoint should
    take the normal path through ``build_agent_system_prompt`` and the
    response should reflect the user's profile/soul rather than the
    bootstrap conversation.
    """
    _create_session(
        test_user,
        "1_700",
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
    resp = client.get("/api/user/sessions/1_700/system-prompt")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "1_700"
    assert data["is_onboarding"] is False
    # Normal system prompt has the standard preamble; bootstrap doesn't
    # contain this exact phrase.
    assert "AI assistant for solo tradespeople" in data["system_prompt"]
    assert "blank slate" not in data["system_prompt"]


def test_get_system_prompt_returns_bootstrap_during_onboarding(
    client: TestClient,
) -> None:
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

    # Create BOOTSTRAP.md so is_onboarding_needed returns True.
    from pathlib import Path

    from backend.app.agent.prompts import load_prompt
    from backend.app.config import settings as _settings

    cdir = Path(_settings.data_dir) / "sp-onboard"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "BOOTSTRAP.md").write_text(load_prompt("bootstrap") + "\n", encoding="utf-8")

    # Override auth to this onboarding user just for this test.
    from backend.app.auth.dependencies import get_current_user
    from backend.app.main import app as _app

    _app.dependency_overrides[get_current_user] = lambda: user
    try:
        _create_session(
            user,
            "sp-onboard-1",
            [{"direction": "inbound", "body": "hey", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
            channel="webchat",
        )
        resp = client.get("/api/user/sessions/sp-onboard-1/system-prompt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_onboarding"] is True
        # Bootstrap-flavored prompt has these phrases verbatim.
        assert "blank slate" in data["system_prompt"]
    finally:
        _app.dependency_overrides.pop(get_current_user, None)


def test_get_system_prompt_session_not_found(client: TestClient) -> None:
    resp = client.get("/api/user/sessions/nonexistent/system-prompt")
    assert resp.status_code == 404


def test_get_system_prompt_cross_user_isolation(client: TestClient, test_user: User) -> None:
    """A session owned by a different user must 404, not leak its prompt."""
    db = _db_module.SessionLocal()
    try:
        other = User(
            id="sp-other",
            user_id="other-user",
            channel_identifier="555700002",
            preferred_channel="webchat",
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
        "sp-other-session",
        [{"direction": "inbound", "body": "secret", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
        channel="webchat",
    )
    # Auth fixture sets request user to test_user, not "sp-other"
    resp = client.get("/api/user/sessions/sp-other-session/system-prompt")
    assert resp.status_code == 404


def test_get_system_prompt_handles_empty_session(client: TestClient, test_user: User) -> None:
    """A session with no messages still returns a valid prompt (empty memory query)."""
    _create_session(test_user, "1_800", [], channel="webchat")
    resp = client.get("/api/user/sessions/1_800/system-prompt")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "1_800"
    assert data["system_prompt"]


def test_get_system_prompt_omits_storage_and_outbound_tool_usage_hints(
    client: TestClient, test_user: User
) -> None:
    """Documenting an intentional preview/runtime divergence.

    The tool registry filters factories whose ``requires_storage`` or
    ``requires_outbound`` flag is set when the ToolContext doesn't
    provide the matching dependency. The preview path passes ``None``
    for both (see ``build_initial_turn_tools``) because it can't
    safely construct a real storage backend or outbound-publish hook.

    Consequence: ``send_media_reply`` (``requires_outbound=True``) and
    ``upload_to_storage`` / ``organize_file`` (``requires_storage=True``)
    have their per-tool ``usage_hint`` strings absent from the rendered
    Tool Guidelines section even though the agent runtime sees them.
    The tool names themselves still appear in the prompt because the
    static ``instructions.md`` references them, so the LLM (and a
    reader) knows the tools exist. The granular usage hints are what
    diverges.

    This test pins the behavior so the gap is visible. If a future
    change makes the preview honest about these usage hints, update
    the assertions accordingly.
    """
    _create_session(
        test_user,
        "1_900",
        [{"direction": "inbound", "body": "hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
        channel="webchat",
    )
    resp = client.get("/api/user/sessions/1_900/system-prompt")
    assert resp.status_code == 200
    prompt = resp.json()["system_prompt"]
    # Usage-hint strings unique to the requires_storage / requires_outbound
    # tool factories. If the preview ever instantiates these factories
    # (option A or C from PR review), these strings will appear and this
    # test should be flipped to assert presence.
    assert "When sending estimates or files, use this to send media" not in prompt
    assert "Upload a recently received file to cloud storage" not in prompt
    assert "Move an unsorted file into the correct client folder" not in prompt


# ---------------------------------------------------------------------------
# DELETE /api/user/sessions/{session_id}/messages/{seq}
# ---------------------------------------------------------------------------


def test_delete_single_message(client: TestClient, test_user: User) -> None:
    """Deleting a single message removes only that message."""
    _create_session(
        test_user,
        "del_single_1",
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
    # Delete message seq=2
    resp = client.delete("/api/user/sessions/del_single_1/messages/2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["seq"] == 2

    # Verify only that message was removed
    resp = client.get("/api/user/sessions/del_single_1")
    assert resp.status_code == 200
    detail = resp.json()
    seqs = [m["seq"] for m in detail["messages"]]
    assert seqs == [1, 3]


def test_delete_single_message_not_found_session(client: TestClient) -> None:
    """Deleting a message from a nonexistent session returns 404."""
    resp = client.delete("/api/user/sessions/nonexistent/messages/1")
    assert resp.status_code == 404


def test_delete_single_message_not_found_seq(client: TestClient, test_user: User) -> None:
    """Deleting a nonexistent seq from a valid session returns 404."""
    _create_session(
        test_user,
        "del_single_noseq",
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    resp = client.delete("/api/user/sessions/del_single_noseq/messages/99")
    assert resp.status_code == 404


def test_delete_single_message_cross_user_isolation(client: TestClient, test_user: User) -> None:
    """A user cannot delete a message from another user's session."""
    other_user_id = "other-user-single-delete-test"
    db = _db_module.SessionLocal()
    try:
        other_user = User(
            id=other_user_id,
            user_id="other-user-sd",
            phone="+15558888888",
            channel_identifier="888888",
        )
        db.add(other_user)
        db.flush()
        cs = ChatSession(
            session_id="other_single_del",
            user_id=other_user_id,
            is_active=True,
            channel="",
            last_compacted_seq=0,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        msg = Message(
            session_id=cs.id,
            seq=1,
            direction="inbound",
            body="secret",
            timestamp=datetime(2025, 1, 15, 10, 1, 0, tzinfo=UTC),
        )
        db.add(msg)
        db.commit()
    finally:
        db.close()

    # Authenticated as test_user, try to delete other user's message
    resp = client.delete("/api/user/sessions/other_single_del/messages/1")
    assert resp.status_code == 404

    # Verify the message is still intact
    db = _db_module.SessionLocal()
    try:
        cs = db.query(ChatSession).filter_by(session_id="other_single_del").first()
        assert cs is not None
        count = db.query(Message).filter_by(session_id=cs.id).count()
        assert count == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# DELETE /api/user/sessions/{session_id}/messages/batch
# ---------------------------------------------------------------------------


def test_delete_batch_messages(client: TestClient, test_user: User) -> None:
    """Batch deleting specific messages removes only those messages."""
    _create_session(
        test_user,
        "del_batch_1",
        [
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {"direction": "outbound", "body": "Hey", "timestamp": "2025-01-15T10:02:00", "seq": 2},
            {"direction": "inbound", "body": "Q", "timestamp": "2025-01-15T10:03:00", "seq": 3},
            {"direction": "outbound", "body": "A", "timestamp": "2025-01-15T10:04:00", "seq": 4},
            {"direction": "inbound", "body": "Bye", "timestamp": "2025-01-15T10:05:00", "seq": 5},
        ],
    )
    resp = client.request(
        "DELETE", "/api/user/sessions/del_batch_1/messages/batch", json={"seqs": [2, 3, 4]}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["messages_deleted"] == 3

    # Verify only messages 1 and 5 remain
    resp = client.get("/api/user/sessions/del_batch_1")
    detail = resp.json()
    seqs = [m["seq"] for m in detail["messages"]]
    assert seqs == [1, 5]


def test_delete_batch_partial(client: TestClient, test_user: User) -> None:
    """Batch delete with some nonexistent seqs deletes only existing ones."""
    _create_session(
        test_user,
        "del_batch_partial",
        [
            {"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {"direction": "outbound", "body": "Hey", "timestamp": "2025-01-15T10:02:00", "seq": 2},
            {"direction": "inbound", "body": "Bye", "timestamp": "2025-01-15T10:03:00", "seq": 3},
        ],
    )
    resp = client.request(
        "DELETE", "/api/user/sessions/del_batch_partial/messages/batch", json={"seqs": [1, 2, 99]}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_deleted"] == 2

    resp = client.get("/api/user/sessions/del_batch_partial")
    detail = resp.json()
    seqs = [m["seq"] for m in detail["messages"]]
    assert seqs == [3]


def test_delete_batch_empty_seqs(client: TestClient, test_user: User) -> None:
    """Batch delete with empty seqs returns 422 validation error."""
    _create_session(
        test_user,
        "del_batch_empty",
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    resp = client.request(
        "DELETE", "/api/user/sessions/del_batch_empty/messages/batch", json={"seqs": []}
    )
    assert resp.status_code == 422


def test_delete_batch_session_not_found(client: TestClient) -> None:
    """Batch delete on a nonexistent session returns 404."""
    resp = client.request(
        "DELETE", "/api/user/sessions/nonexistent/messages/batch", json={"seqs": [1, 2]}
    )
    assert resp.status_code == 404


def test_delete_batch_cross_user(client: TestClient, test_user: User) -> None:
    """A user cannot batch delete messages from another user's session."""
    other_user_id = "other-user-batch-delete-test"
    db = _db_module.SessionLocal()
    try:
        other_user = User(
            id=other_user_id,
            user_id="other-user-bd",
            phone="+15557777777",
            channel_identifier="777777",
        )
        db.add(other_user)
        db.flush()
        cs = ChatSession(
            session_id="other_batch_del",
            user_id=other_user_id,
            is_active=True,
            channel="",
            last_compacted_seq=0,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        for i in range(1, 4):
            msg = Message(
                session_id=cs.id,
                seq=i,
                direction="inbound",
                body=f"secret {i}",
                timestamp=datetime(2025, 1, 15, 10, i, 0, tzinfo=UTC),
            )
            db.add(msg)
        db.commit()
    finally:
        db.close()

    # Authenticated as test_user, try to batch delete other user's messages
    resp = client.request(
        "DELETE", "/api/user/sessions/other_batch_del/messages/batch", json={"seqs": [1, 2]}
    )
    assert resp.status_code == 404

    # Verify messages are still intact
    db = _db_module.SessionLocal()
    try:
        cs = db.query(ChatSession).filter_by(session_id="other_batch_del").first()
        assert cs is not None
        count = db.query(Message).filter_by(session_id=cs.id).count()
        assert count == 3
    finally:
        db.close()


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
    _create_session(test_user, "del_batch_large", msgs)
    seqs_to_delete = list(range(1, 51))
    resp = client.request(
        "DELETE", "/api/user/sessions/del_batch_large/messages/batch", json={"seqs": seqs_to_delete}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_deleted"] == 50

    resp = client.get("/api/user/sessions/del_batch_large")
    detail = resp.json()
    assert len(detail["messages"]) == 0


# ---------------------------------------------------------------------------
# DELETE /api/user/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


def test_delete_conversation_history(client: TestClient, test_user: User) -> None:
    """Deleting conversation history removes messages but preserves the session."""
    _create_session(
        test_user,
        "del_1",
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
    # Delete messages
    resp = client.delete("/api/user/sessions/del_1/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["messages_deleted"] == 2

    # Session still exists but has no messages
    resp = client.get("/api/user/sessions/del_1")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["session_id"] == "del_1"
    assert len(detail["messages"]) == 0
    assert detail["last_compacted_seq"] == 0
    assert detail["initial_system_prompt"] == ""


def test_delete_conversation_history_preserves_memory(client: TestClient, test_user: User) -> None:
    """Memory documents are not affected by conversation history deletion."""
    from backend.app.agent.memory_db import get_memory_store

    _create_session(
        test_user,
        "del_mem",
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
    )
    # Write something to memory
    mem_store = get_memory_store(test_user.id)
    mem_store.write_memory("# Test Memory\nImportant fact.")

    # Delete conversation history
    resp = client.delete("/api/user/sessions/del_mem/messages")
    assert resp.status_code == 200

    # Memory is intact
    content = mem_store.read_memory()
    assert "Important fact." in content


def test_delete_conversation_history_not_found(client: TestClient) -> None:
    """Deleting messages from a nonexistent session returns 404."""
    resp = client.delete("/api/user/sessions/nonexistent/messages")
    assert resp.status_code == 404


def test_delete_conversation_history_empty_session(client: TestClient, test_user: User) -> None:
    """Deleting messages from a session with no messages returns 0 deleted."""
    _create_session(test_user, "del_empty", [])
    resp = client.delete("/api/user/sessions/del_empty/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_deleted"] == 0


def test_delete_conversation_history_cross_user_isolation(
    client: TestClient, test_user: User
) -> None:
    """A user cannot delete another user's conversation history."""
    # Create a session owned by a different user
    other_user_id = "other-user-for-isolation-test"
    db = _db_module.SessionLocal()
    try:
        other_user = User(
            id=other_user_id,
            user_id="other-user",
            phone="+15559999999",
            channel_identifier="999999",
        )
        db.add(other_user)
        db.flush()
        cs = ChatSession(
            session_id="other_session",
            user_id=other_user_id,
            is_active=True,
            channel="",
            last_compacted_seq=0,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        msg = Message(
            session_id=cs.id,
            seq=1,
            direction="inbound",
            body="secret",
            timestamp=datetime(2025, 1, 15, 10, 1, 0, tzinfo=UTC),
        )
        db.add(msg)
        db.commit()
    finally:
        db.close()

    # Authenticated as test_user, try to delete other user's session
    resp = client.delete("/api/user/sessions/other_session/messages")
    assert resp.status_code == 404

    # Verify the other user's message is still intact
    db = _db_module.SessionLocal()
    try:
        cs = db.query(ChatSession).filter_by(session_id="other_session").first()
        assert cs is not None
        count = db.query(Message).filter_by(session_id=cs.id).count()
        assert count == 1
    finally:
        db.close()
