"""Tests for /api/user/heartbeat-logs endpoints (GET and DELETE)."""

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import HeartbeatLog, User


def _create_heartbeat_log(
    user_id: str,
    action_type: str = "send",
    message_text: str = "",
    channel: str = "",
    reasoning: str = "",
    tasks: str = "",
) -> None:
    db = _db_module.SessionLocal()
    try:
        db.add(
            HeartbeatLog(
                user_id=user_id,
                action_type=action_type,
                message_text=message_text,
                channel=channel,
                reasoning=reasoning,
                tasks=tasks,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
    finally:
        db.close()


def _create_other_user() -> str:
    """Create a second user and return their id."""
    db = _db_module.SessionLocal()
    try:
        other = User(
            id=str(uuid.uuid4()),
            user_id="other-user",
            phone="+15550000000",
            channel_identifier="999999999",
            preferred_channel="telegram",
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        return other.id
    finally:
        db.close()


def test_heartbeat_logs_empty(client: TestClient) -> None:
    """Returns empty list when no heartbeat logs exist."""
    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_heartbeat_logs_with_data(client: TestClient, test_user: User) -> None:
    """Returns heartbeat logs for the current user."""
    _create_heartbeat_log(test_user.id)
    _create_heartbeat_log(test_user.id)

    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Most recent first
    assert data["items"][0]["id"] > data["items"][1]["id"]
    assert data["items"][0]["user_id"] == test_user.id


def test_heartbeat_logs_limit(client: TestClient, test_user: User) -> None:
    """Respects the limit query parameter."""
    for _ in range(5):
        _create_heartbeat_log(test_user.id)

    resp = client.get("/api/user/heartbeat-logs?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2


def test_heartbeat_logs_scoped_to_user(client: TestClient, test_user: User) -> None:
    """Only returns logs for the authenticated user, not other users."""
    other_id = _create_other_user()
    _create_heartbeat_log(test_user.id)
    _create_heartbeat_log(other_id)

    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert all(item["user_id"] == test_user.id for item in data["items"])


def test_heartbeat_logs_enriched_fields(client: TestClient, test_user: User) -> None:
    """Returns enriched fields (action_type, message_text, channel, reasoning, tasks)."""
    _create_heartbeat_log(
        test_user.id,
        action_type="send",
        message_text="Hello there!",
        channel="telegram",
        reasoning="User has a pending task",
        tasks="Check invoice status",
    )
    _create_heartbeat_log(
        test_user.id,
        action_type="skip",
        reasoning="Nothing to do right now",
    )

    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    items = data["items"]

    # Most recent first (the skip)
    skip_item = items[0]
    assert skip_item["action_type"] == "skip"
    assert skip_item["reasoning"] == "Nothing to do right now"
    assert skip_item["message_text"] == ""

    send_item = items[1]
    assert send_item["action_type"] == "send"
    assert send_item["message_text"] == "Hello there!"
    assert send_item["channel"] == "telegram"
    assert send_item["reasoning"] == "User has a pending task"
    assert send_item["tasks"] == "Check invoice status"


# ---------------------------------------------------------------------------
# DELETE /api/user/heartbeat-logs
# ---------------------------------------------------------------------------


def test_delete_heartbeat_logs(client: TestClient, test_user: User) -> None:
    """Deletes all heartbeat logs for the current user and returns count."""
    _create_heartbeat_log(test_user.id, message_text="msg1")
    _create_heartbeat_log(test_user.id, message_text="msg2")
    _create_heartbeat_log(test_user.id, action_type="skip")

    resp = client.delete("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["deleted"] == 3

    # Verify logs are gone
    get_resp = client.get("/api/user/heartbeat-logs")
    assert get_resp.json()["total"] == 0


def test_delete_heartbeat_logs_empty(client: TestClient) -> None:
    """Returns 0 when there are no logs to delete."""
    resp = client.delete("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["deleted"] == 0


def test_delete_heartbeat_logs_cross_user_isolation(client: TestClient, test_user: User) -> None:
    """Only deletes logs belonging to the authenticated user."""
    other_id = _create_other_user()
    _create_heartbeat_log(test_user.id, message_text="mine")
    _create_heartbeat_log(other_id, message_text="theirs")

    resp = client.delete("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    # Other user's logs should still exist
    db = _db_module.SessionLocal()
    try:
        remaining = db.query(HeartbeatLog).filter(HeartbeatLog.user_id == other_id).count()
        assert remaining == 1
    finally:
        db.close()
