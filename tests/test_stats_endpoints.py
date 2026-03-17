"""Tests for user stats endpoint."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import ChatSession, Message, User


def test_stats_empty(client: TestClient) -> None:
    resp = client.get("/api/user/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_sessions"] == 0
    assert data["messages_this_month"] == 0
    # Default HEARTBEAT.md is seeded with items on user creation
    assert data["active_heartbeat_items"] >= 0
    assert data["total_memory_facts"] == 0
    assert data["last_conversation_at"] is None


def test_stats_with_data(client: TestClient, test_user: User) -> None:
    # Create a session with messages in the DB
    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id="1_100",
            user_id=test_user.id,
            is_active=True,
            channel="",
            last_compacted_seq=0,
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            last_message_at=datetime(2025, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        db.add(cs)
        db.flush()
        db.add(
            Message(
                session_id=cs.id,
                seq=1,
                direction="inbound",
                body="Hello",
                timestamp=datetime(2025, 1, 15, 10, 1, 0, tzinfo=UTC),
            )
        )
        db.commit()
    finally:
        db.close()

    # Create a heartbeat item
    client.post("/api/user/heartbeat", json={"description": "Check site"})

    # Create memory
    from backend.app.agent.memory_db import get_memory_store

    store = get_memory_store(test_user.id)
    store.write_memory("# Long-term Memory\n\n## General\n- rate: 85 (confidence: 1.0)")

    resp = client.get("/api/user/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_sessions"] == 1
    # 3 default items + 1 added = 4
    assert data["active_heartbeat_items"] >= 1
    assert data["total_memory_facts"] == 1
    assert data["last_conversation_at"] is not None
