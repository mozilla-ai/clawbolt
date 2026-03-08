"""Tests for conversation session endpoints."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import ContractorData
from backend.app.config import settings


def _create_session(
    contractor: ContractorData, session_id: str, messages: list[dict[str, object]]
) -> None:
    """Create a test session JSONL file."""
    base = Path(settings.contractor_data_dir) / str(contractor.id) / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{session_id}.jsonl"
    lines = [
        json.dumps(
            {
                "_type": "metadata",
                "session_id": session_id,
                "contractor_id": contractor.id,
                "created_at": "2025-01-15T10:00:00+00:00",
                "last_message_at": "2025-01-15T10:05:00+00:00",
                "is_active": True,
                "last_compacted_seq": 0,
            }
        )
    ]
    for msg in messages:
        lines.append(json.dumps(msg))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_list_sessions_empty(client: TestClient) -> None:
    resp = client.get("/api/contractor/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert data["total"] == 0


def test_list_sessions(client: TestClient, test_contractor: ContractorData) -> None:
    _create_session(
        test_contractor,
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
    resp = client.get("/api/contractor/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["message_count"] == 2
    assert data["sessions"][0]["id"] == "1_100"


def test_get_session_detail(client: TestClient, test_contractor: ContractorData) -> None:
    tool_json = json.dumps([{"tool": "save_fact", "input": {"key": "rate"}, "result": "saved"}])
    _create_session(
        test_contractor,
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
    resp = client.get("/api/contractor/sessions/1_200")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "1_200"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["tool_interactions"] == []
    assert len(data["messages"][1]["tool_interactions"]) == 1
    assert data["messages"][1]["tool_interactions"][0]["tool"] == "save_fact"


def test_session_direction_values(client: TestClient, test_contractor: ContractorData) -> None:
    """API response direction values must be 'inbound'/'outbound' (not 'incoming'/'outgoing')."""
    _create_session(
        test_contractor,
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
    resp = client.get("/api/contractor/sessions/1_300")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"][0]["direction"] == "inbound"
    assert data["messages"][1]["direction"] == "outbound"


def test_get_session_not_found(client: TestClient) -> None:
    resp = client.get("/api/contractor/sessions/nonexistent")
    assert resp.status_code == 404


def test_list_sessions_pagination(client: TestClient, test_contractor: ContractorData) -> None:
    for i in range(5):
        _create_session(
            test_contractor,
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
    resp = client.get("/api/contractor/sessions?offset=0&limit=2")
    data = resp.json()
    assert data["total"] == 5
    assert len(data["sessions"]) == 2
