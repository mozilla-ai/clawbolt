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
            ts_str = msg_data.get("timestamp", "")
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


def test_list_sessions_empty(client: TestClient) -> None:
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert data["total"] == 0


def test_list_sessions(client: TestClient, test_user: User) -> None:
    _create_session(
        test_user,
        "1_100",
        [
            {"direction": "inbound", "body": "Hello", "timestamp": "2025-01-15T10:01:00", "seq": 1},
            {
                "direction": "outbound",
                "body": "Hi there!",
                "timestamp": "2025-01-15T10:02:00",
                "seq": 2,
            },
        ],
    )
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["message_count"] == 2
    assert data["sessions"][0]["id"] == "1_100"


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


def test_list_sessions_pagination(client: TestClient, test_user: User) -> None:
    for i in range(5):
        _create_session(
            test_user,
            f"1_{i}",
            [
                {
                    "direction": "inbound",
                    "body": f"msg {i}",
                    "timestamp": f"2025-01-15T10:0{i}:00",
                    "seq": 1,
                }
            ],
        )
    resp = client.get("/api/user/sessions?offset=0&limit=2")
    data = resp.json()
    assert data["total"] == 5
    assert len(data["sessions"]) == 2


def test_session_list_includes_channel(client: TestClient, test_user: User) -> None:
    """Session list should include the channel field when present in metadata."""
    _create_session(
        test_user,
        "1_400",
        [{"direction": "inbound", "body": "Hi", "timestamp": "2025-01-15T10:01:00", "seq": 1}],
        channel="telegram",
    )
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"][0]["channel"] == "telegram"


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
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"][0]["channel"] == ""

    resp = client.get("/api/user/sessions/1_600")
    assert resp.status_code == 200
    data = resp.json()
    assert data["channel"] == ""
