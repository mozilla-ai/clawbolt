"""Tests for memory/facts endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import UserData
from backend.app.config import settings


def _seed_memory(user: UserData) -> None:
    """Create a MEMORY.md with test data."""
    mem_dir = Path(settings.data_dir) / str(user.id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text(
        "# Long-term Memory\n\n"
        "## Business\n"
        "- hourly_rate: 75 (confidence: 1.0)\n"
        "- trade: plumbing (confidence: 0.9)\n\n"
        "## Personal\n"
        "- name: Mike (confidence: 1.0)\n",
        encoding="utf-8",
    )


def test_list_memory_empty(client: TestClient) -> None:
    resp = client.get("/api/user/memory")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_memory(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.get("/api/user/memory")
    assert resp.status_code == 200
    facts = resp.json()
    assert len(facts) == 3
    keys = {f["key"] for f in facts}
    assert "hourly_rate" in keys
    assert "name" in keys


def test_list_memory_filter_category(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.get("/api/user/memory?category=business")
    assert resp.status_code == 200
    facts = resp.json()
    assert len(facts) == 2
    assert all(f["category"] == "business" for f in facts)


def test_update_memory(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.put(
        "/api/user/memory/hourly_rate",
        json={"value": "85"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] == "85"
    assert data["key"] == "hourly_rate"


def test_update_memory_not_found(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.put(
        "/api/user/memory/nonexistent",
        json={"value": "test"},
    )
    assert resp.status_code == 404


def test_delete_memory(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.delete("/api/user/memory/hourly_rate")
    assert resp.status_code == 204
    # Verify it's gone
    resp = client.get("/api/user/memory")
    keys = {f["key"] for f in resp.json()}
    assert "hourly_rate" not in keys


def test_delete_memory_not_found(client: TestClient) -> None:
    resp = client.delete("/api/user/memory/nonexistent")
    assert resp.status_code == 404
