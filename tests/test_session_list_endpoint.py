"""Tests for GET /api/user/sessions endpoint.

Each user has at most one session (enforced by ``uq_sessions_user_id``),
so this endpoint returns either an empty list or a single-item list.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import ChatSession, Message, User


def _create_session(
    user_id: str,
    *,
    channel: str = "telegram",
    message_count: int = 0,
    last_message_at: datetime | None = None,
) -> ChatSession:
    """Create the user's single ChatSession with optional messages."""
    db = _db_module.SessionLocal()
    try:
        now = last_message_at or datetime.now(UTC)
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=user_id,
            channel=channel,
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
    """Returns empty list when no session exists yet."""
    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_sessions_with_data(client: TestClient, test_user: User) -> None:
    """Returns the user's single session with its message count."""
    _create_session(test_user.id, message_count=3)

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["message_count"] == 3


def test_sessions_scoped_to_user(client: TestClient, test_user: User) -> None:
    """Only returns the session for the authenticated user."""
    other_id = _create_other_user()
    _create_session(test_user.id, message_count=1)
    _create_session(other_id, message_count=5)

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["message_count"] == 1


def test_sessions_fields(client: TestClient, test_user: User) -> None:
    """The session item contains the expected fields."""
    _create_session(test_user.id, channel="telegram", message_count=2)

    resp = client.get("/api/user/sessions")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert "session_id" in item
    assert item["channel"] == "telegram"
    assert item["message_count"] == 2
    assert "created_at" in item
    assert "last_message_at" in item
