"""Tests for memory endpoint (freeform MEMORY.md)."""

from fastapi.testclient import TestClient

from backend.app.agent.memory_db import get_memory_store
from backend.app.models import User


def _seed_memory(user: User) -> None:
    """Create memory with test data."""
    store = get_memory_store(user.id)
    store.write_memory("## Pricing\n- Deck: $45/sqft\n- Fence: $20/ft")


def test_get_memory_empty(client: TestClient) -> None:
    resp = client.get("/api/user/memory")
    assert resp.status_code == 200
    assert resp.json() == {"content": ""}


def test_get_memory(client: TestClient, test_user: User) -> None:
    _seed_memory(test_user)
    resp = client.get("/api/user/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert "Deck: $45/sqft" in data["content"]
    assert "Fence: $20/ft" in data["content"]


def test_update_memory(client: TestClient, test_user: User) -> None:
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
