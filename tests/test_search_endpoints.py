"""Tests for the unified search endpoint."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import UserData
from backend.app.config import settings


def _create_session(
    user: UserData,
    session_id: str,
    messages: list[dict[str, object]],
) -> None:
    """Create a test session JSONL file."""
    base = Path(settings.data_dir) / str(user.id) / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{session_id}.jsonl"
    metadata: dict[str, object] = {
        "_type": "metadata",
        "session_id": session_id,
        "user_id": user.id,
        "created_at": "2025-01-15T10:00:00+00:00",
        "last_message_at": "2025-01-15T10:05:00+00:00",
        "is_active": True,
        "last_compacted_seq": 0,
    }
    lines = [json.dumps(metadata)]
    for msg in messages:
        lines.append(json.dumps(msg))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _create_memory(user: UserData, facts: list[tuple[str, str, str]]) -> None:
    """Create a MEMORY.md file with given (key, value, category) triples."""
    mem_dir = Path(settings.data_dir) / str(user.id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Long-term Memory\n"]
    current_cat = ""
    for key, value, category in facts:
        if category != current_cat:
            current_cat = category
            lines.append(f"\n## {category}\n")
        lines.append(f"- {key}: {value} (confidence: 1.0)\n")
    (mem_dir / "MEMORY.md").write_text("".join(lines), encoding="utf-8")


def _create_clients(user: UserData, clients: list[dict[str, str]]) -> None:
    """Create a clients.json file."""
    base = Path(settings.data_dir) / str(user.id)
    base.mkdir(parents=True, exist_ok=True)
    path = base / "clients.json"
    path.write_text(json.dumps(clients, indent=2), encoding="utf-8")


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


def test_search_finds_memory_facts(client: TestClient, test_user: UserData) -> None:
    _create_memory(
        test_user,
        [
            ("hourly_rate", "$85/hr", "pricing"),
            ("favorite_tool", "DeWalt drill", "tools"),
        ],
    )
    resp = client.get("/api/search?q=dewalt")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["type"] == "memory"
    assert data["results"][0]["title"] == "favorite_tool"


def test_search_finds_sessions(client: TestClient, test_user: UserData) -> None:
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


def test_search_finds_clients(client: TestClient, test_user: UserData) -> None:
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


def test_search_case_insensitive(client: TestClient, test_user: UserData) -> None:
    _create_memory(test_user, [("City", "Portland", "location")])
    resp = client.get("/api/search?q=portland")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1

    resp = client.get("/api/search?q=PORTLAND")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_search_mixed_results(client: TestClient, test_user: UserData) -> None:
    _create_memory(test_user, [("kitchen_style", "modern kitchen", "preferences")])
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
