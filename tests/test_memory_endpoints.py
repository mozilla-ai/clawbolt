"""Tests for memory endpoint (freeform MEMORY.md)."""

from fastapi.testclient import TestClient

from backend.app.database import SessionLocal
from backend.app.models import MemoryDocument, User


def _seed_memory(user: User) -> None:
    """Create memory with test data via direct ORM write.

    The MemoryStore API is async-only now (issue #1160 follow-up); sync
    test helpers seed the row directly so they can run from inside a
    sync ``TestClient`` test without spinning up an event loop.
    """
    db = SessionLocal()
    try:
        doc = db.query(MemoryDocument).filter_by(user_id=user.id).one_or_none()
        text = "## Pricing\n- Deck: $45/sqft\n- Fence: $20/ft\n"
        if doc is None:
            doc = MemoryDocument(user_id=user.id, memory_text=text, history_text="")
            db.add(doc)
        else:
            doc.memory_text = text
        db.commit()
    finally:
        db.close()


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
