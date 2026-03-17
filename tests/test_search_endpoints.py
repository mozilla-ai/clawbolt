"""Tests for the unified search endpoint."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.models import ChatSession, Message, User


def _create_session(
    user: User,
    session_id: str,
    messages: list[dict[str, object]],
) -> None:
    """Create a session with messages in the database."""
    db = _db_module.SessionLocal()
    try:
        cs = ChatSession(
            session_id=session_id,
            user_id=user.id,
            is_active=True,
            channel="",
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
                timestamp=ts,
            )
            db.add(msg)
        db.commit()
    finally:
        db.close()


def _create_memory(user: User, content: str) -> None:
    """Create memory text in the database."""
    from backend.app.agent.memory_db import get_memory_store

    store = get_memory_store(user.id)
    store.write_memory(content)


def _create_clients(user: User, clients: list[dict[str, str]]) -> None:
    """Create Client rows in the database."""
    from backend.app.models import Client

    db = _db_module.SessionLocal()
    try:
        for c in clients:
            db.add(
                Client(
                    id=c.get("id", "client"),
                    user_id=user.id,
                    name=c.get("name", ""),
                    phone=c.get("phone", ""),
                    email=c.get("email", ""),
                    address=c.get("address", ""),
                    notes=c.get("notes", ""),
                )
            )
        db.commit()
    finally:
        db.close()


def test_search_empty_query(client: TestClient) -> None:
    resp = client.get("/api/search?q=")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert data["query"] == ""


def test_search_no_results(client: TestClient) -> None:
    resp = client.get("/api/search?q=xyznonexistent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []


def test_search_finds_memory_facts(client: TestClient, test_user: User) -> None:
    _create_memory(
        test_user,
        "- Hourly rate: $85/hr\n- Favorite tool: DeWalt drill\n",
    )
    resp = client.get("/api/search?q=dewalt")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["type"] == "memory"
    assert "DeWalt drill" in data["results"][0]["title"]


def test_search_finds_sessions(client: TestClient, test_user: User) -> None:
    _create_session(
        test_user,
        "1_100",
        [
            {
                "direction": "inbound",
                "body": "I need a plumbing estimate",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
            },
        ],
    )
    resp = client.get("/api/search?q=plumbing")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["type"] == "conversation"
    assert "plumbing" in data["results"][0]["preview"].lower()


def test_search_finds_clients(client: TestClient, test_user: User) -> None:
    _create_clients(
        test_user,
        [
            {
                "id": "john-doe",
                "name": "John Doe",
                "phone": "555-1234",
                "email": "",
                "address": "",
                "notes": "",
            },
        ],
    )
    resp = client.get("/api/search?q=john")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["type"] == "client"
    assert data["results"][0]["title"] == "John Doe"


def test_search_case_insensitive(client: TestClient, test_user: User) -> None:
    _create_memory(test_user, "- City: Portland\n")
    resp = client.get("/api/search?q=portland")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1

    resp = client.get("/api/search?q=PORTLAND")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_search_mixed_results(client: TestClient, test_user: User) -> None:
    _create_memory(test_user, "- Kitchen style: modern kitchen\n")
    _create_session(
        test_user,
        "1_200",
        [
            {
                "direction": "inbound",
                "body": "Kitchen remodel quote please",
                "timestamp": "2025-01-15T10:01:00",
                "seq": 1,
            },
        ],
    )
    resp = client.get("/api/search?q=kitchen")
    assert resp.status_code == 200
    data = resp.json()
    types = {r["type"] for r in data["results"]}
    assert "memory" in types
    assert "conversation" in types
