"""Tests for memory endpoint (freeform MEMORY.md)."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.database import db_session_async
from backend.app.models import MemoryDocument, User


def _seed_memory(user: User) -> None:
    """Create memory with test data via direct ORM write.

    Runs the async write through ``asyncio.run`` so sync ``TestClient``
    tests can call it without spinning their own event loop.
    """

    async def _write() -> None:
        async with db_session_async() as db:
            doc = (
                await db.execute(select(MemoryDocument).filter_by(user_id=user.id))
            ).scalar_one_or_none()
            text = "## Pricing\n- Deck: $45/sqft\n- Fence: $20/ft\n"
            if doc is None:
                doc = MemoryDocument(user_id=user.id, memory_text=text, history_text="")
                db.add(doc)
            else:
                doc.memory_text = text
            await db.commit()

    asyncio.run(_write())


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
