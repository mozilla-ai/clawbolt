"""Tests for GET /api/user/sessions endpoint."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import ChatSession, Message, User


def _create_session(
    user_id: str,
    *,
    channel: str = "telegram",
    is_active: bool = True,
    message_count: int = 0,
    last_message_at: datetime | None = None,
) -> ChatSession:
    """Create a ChatSession with optional messages."""
    db = _db_module.SessionLocal()
    try:
        now = last_message_at or datetime.now(UTC)
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=user_id,
            is_active=is_active,
            channel=channel,
            last_compacted_seq=0,
            created_at=now - timedelta(hours=1),
            last_message_at=now,
        )
        db.add(cs)
        db.flush()

        for i in range(message_count):
            db.add(
                Message(
                    session_id=cs.id,
                    seq=i + 1,
                    direction="inbound" if i % 2 == 0 else "outbound",
                    body=f"Message {i + 1}",
                    timestamp=now - timedelta(minutes=message_count - i),
                )
            )
        db.commit()
        db.refresh(cs)
        return cs
    finally:
        db.close()


def _create_other_user() -> str:
    db = _db_module.SessionLocal()
    try:
        other = User(
            id=str(uuid.uuid4()),
            user_id="other-session-user",
            phone="+15550001111",
            channel_identifier="888888888",
            preferred_channel="telegram",
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        return other.id
    finally:
        db.close()


def test_sessions_empty(client: TestClient) -> None:
    """Returns empty list when no sessions exist."""
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_sessions_with_data(client: TestClient, test_user: User) -> None:
    """Returns sessions with message counts."""
    _create_session(test_user.id, message_count=3)
    _create_session(test_user.id, message_count=0, channel="webchat")

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Check message counts
    counts = {item["message_count"] for item in data["items"]}
    assert counts == {0, 3}


def test_sessions_ordered_by_last_message(client: TestClient, test_user: User) -> None:
    """Sessions are ordered by last_message_at descending."""
    now = datetime.now(UTC)
    _create_session(test_user.id, last_message_at=now - timedelta(hours=2))
    _create_session(test_user.id, last_message_at=now)

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    # Most recent first
    assert items[0]["last_message_at"] > items[1]["last_message_at"]


def test_sessions_scoped_to_user(client: TestClient, test_user: User) -> None:
    """Only returns sessions for the authenticated user."""
    other_id = _create_other_user()
    _create_session(test_user.id, message_count=1)
    _create_session(other_id, message_count=5)

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["message_count"] == 1


def test_sessions_pagination(client: TestClient, test_user: User) -> None:
    """Respects limit and offset parameters."""
    for _ in range(5):
        _create_session(test_user.id, message_count=1)

    resp = client.get("/api/user/sessions?limit=2&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2

    resp2 = client.get("/api/user/sessions?limit=2&offset=2")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["items"]) == 2


def test_sessions_fields(client: TestClient, test_user: User) -> None:
    """Each session item contains expected fields."""
    _create_session(test_user.id, channel="telegram", is_active=True, message_count=2)

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert "session_id" in item
    assert item["channel"] == "telegram"
    assert item["is_active"] is True
    assert item["message_count"] == 2
    assert "created_at" in item
    assert "last_message_at" in item


def test_sessions_is_active_filter(client: TestClient, test_user: User) -> None:
    """Filters sessions by is_active query parameter."""
    _create_session(test_user.id, is_active=True, message_count=1)
    _create_session(test_user.id, is_active=True, message_count=1)
    _create_session(test_user.id, is_active=False, message_count=1)

    # Filter active only
    resp = client.get("/api/user/sessions?is_active=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(item["is_active"] for item in data["items"])

    # Filter closed only
    resp = client.get("/api/user/sessions?is_active=false")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert not data["items"][0]["is_active"]

    # No filter returns all
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    assert resp.json()["total"] == 3
