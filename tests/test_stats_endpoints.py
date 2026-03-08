"""Tests for contractor stats endpoint."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import ContractorData
from backend.app.config import settings


def test_stats_empty(client: TestClient) -> None:
    resp = client.get("/api/user/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_sessions"] == 0
    assert data["messages_this_month"] == 0
    assert data["active_checklist_items"] == 0
    assert data["total_memory_facts"] == 0
    assert data["last_conversation_at"] is None


def test_stats_with_data(client: TestClient, test_contractor: ContractorData) -> None:
    # Create a session with messages
    base = Path(settings.data_dir) / str(test_contractor.id) / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "1_100.jsonl"
    meta = {
        "_type": "metadata",
        "session_id": "1_100",
        "contractor_id": test_contractor.id,
        "created_at": "2025-01-15T10:00:00+00:00",
        "last_message_at": "2025-01-15T10:05:00+00:00",
        "is_active": True,
        "last_compacted_seq": 0,
    }
    msg = {"direction": "inbound", "body": "Hello", "timestamp": "2025-01-15T10:01:00", "seq": 1}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(msg) + "\n", encoding="utf-8")

    # Create a checklist item
    client.post("/api/user/checklist", json={"description": "Check site"})

    # Create memory
    mem_dir = Path(settings.data_dir) / str(test_contractor.id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text(
        "# Long-term Memory\n\n## General\n- rate: 85 (confidence: 1.0)\n",
        encoding="utf-8",
    )

    resp = client.get("/api/user/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_sessions"] == 1
    assert data["active_checklist_items"] == 1
    assert data["total_memory_facts"] == 1
    assert data["last_conversation_at"] is not None
