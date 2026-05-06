"""Tests for /api/user/heartbeat-logs endpoints (GET and DELETE)."""

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from backend.app.database import db_session_async
from backend.app.models import HeartbeatLog, User


async def _create_heartbeat_log(
    user_id: str,
    action_type: str = "send",
    message_text: str = "",
    channel: str = "",
    reasoning: str = "",
    tasks: str = "",
) -> None:
    async with db_session_async() as db:
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
        await db.commit()


async def _create_other_user() -> str:
    """Create a second user and return their id."""
    async with db_session_async() as db:
        other = User(
            id=str(uuid.uuid4()),
            user_id="other-user",
            phone="+15550000000",
            channel_identifier="999999999",
            preferred_channel="telegram",
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)
        return other.id


async def test_heartbeat_logs_empty(client: TestClient) -> None:
    """Returns empty list when no heartbeat logs exist."""
    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


async def test_heartbeat_logs_with_data(client: TestClient, test_user: User) -> None:
    """Returns heartbeat logs for the current user."""
    await _create_heartbeat_log(test_user.id)
    await _create_heartbeat_log(test_user.id)

    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Most recent first
    assert data["items"][0]["id"] > data["items"][1]["id"]
    assert data["items"][0]["user_id"] == test_user.id


async def test_heartbeat_logs_limit(client: TestClient, test_user: User) -> None:
    """Respects the limit query parameter."""
    for _ in range(5):
        await _create_heartbeat_log(test_user.id)

    resp = client.get("/api/user/heartbeat-logs?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2


async def test_heartbeat_logs_scoped_to_user(client: TestClient, test_user: User) -> None:
    """Only returns logs for the authenticated user, not other users."""
    other_id = await _create_other_user()
    await _create_heartbeat_log(test_user.id)
    await _create_heartbeat_log(other_id)

    resp = client.get("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert all(item["user_id"] == test_user.id for item in data["items"])


async def test_heartbeat_logs_enriched_fields(client: TestClient, test_user: User) -> None:
    """Returns enriched fields (action_type, message_text, channel, reasoning, tasks)."""
    await _create_heartbeat_log(
        test_user.id,
        action_type="send",
        message_text="Hello there!",
        channel="telegram",
        reasoning="User has a pending task",
        tasks="Check invoice status",
    )
    await _create_heartbeat_log(
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


async def test_delete_heartbeat_logs(client: TestClient, test_user: User) -> None:
    """Deletes all heartbeat logs for the current user and returns count."""
    await _create_heartbeat_log(test_user.id, message_text="msg1")
    await _create_heartbeat_log(test_user.id, message_text="msg2")
    await _create_heartbeat_log(test_user.id, action_type="skip")

    resp = client.delete("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["deleted"] == 3

    # Verify logs are gone
    get_resp = client.get("/api/user/heartbeat-logs")
    assert get_resp.json()["total"] == 0


async def test_delete_heartbeat_logs_empty(client: TestClient) -> None:
    """Returns 0 when there are no logs to delete."""
    resp = client.delete("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["deleted"] == 0


async def test_delete_heartbeat_logs_cross_user_isolation(
    client: TestClient, test_user: User
) -> None:
    """Only deletes logs belonging to the authenticated user."""
    from sqlalchemy import select

    other_id = await _create_other_user()
    await _create_heartbeat_log(test_user.id, message_text="mine")
    await _create_heartbeat_log(other_id, message_text="theirs")

    resp = client.delete("/api/user/heartbeat-logs")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    # Other user's logs should still exist
    async with db_session_async() as db:
        remaining = (
            await db.execute(select(HeartbeatLog).where(HeartbeatLog.user_id == other_id))
        ).all()
        assert len(remaining) == 1
