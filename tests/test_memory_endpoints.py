"""Tests for memory endpoint (freeform MEMORY.md)."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import UserData
from backend.app.config import settings


def _seed_memory(user: UserData) -> None:
    """Create a MEMORY.md with test data."""
    mem_dir = Path(settings.data_dir) / str(user.id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text(
        "## Pricing\n- Deck: $45/sqft\n- Fence: $20/ft\n",
        encoding="utf-8",
    )


def test_get_memory_empty(client: TestClient) -> None:
    resp = client.get("/api/user/memory")
    assert resp.status_code == 200
    assert resp.json() == {"content": ""}


def test_get_memory(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.get("/api/user/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert "Deck: $45/sqft" in data["content"]
    assert "Fence: $20/ft" in data["content"]


def test_update_memory(client: TestClient, test_user: UserData) -> None:
    _seed_memory(test_user)
    resp = client.put(
        "/api/user/memory",
        json={"content": "## Pricing\n- Deck: $50/sqft\n"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "$50/sqft" in data["content"]

    # Verify it persisted
    resp2 = client.get("/api/user/memory")
    assert "$50/sqft" in resp2.json()["content"]
