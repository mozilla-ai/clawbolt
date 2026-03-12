"""Tests for heartbeat_text field via the profile endpoint (HEARTBEAT.md)."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import get_user_store
from backend.app.config import settings


def test_profile_includes_heartbeat_text(client: TestClient) -> None:
    """Profile response should include the heartbeat_text field."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert "heartbeat_text" in data


def test_update_heartbeat_text(client: TestClient) -> None:
    """Saving heartbeat_text via profile update should persist it."""
    heartbeat = "- [ ] Follow up with leads\n- [ ] Check job site"
    resp = client.put(
        "/api/user/profile",
        json={"heartbeat_text": heartbeat},
    )
    assert resp.status_code == 200
    assert resp.json()["heartbeat_text"] == heartbeat


def test_heartbeat_text_writes_heartbeat_md(client: TestClient) -> None:
    """Updating heartbeat_text should create a HEARTBEAT.md file on disk."""
    heartbeat = "- [ ] Review pending estimates"
    resp = client.put("/api/user/profile", json={"heartbeat_text": heartbeat})
    assert resp.status_code == 200

    # Find the user directory (user id=1 is the test user)
    user_dir = Path(settings.data_dir) / "1"
    heartbeat_path = user_dir / "HEARTBEAT.md"
    assert heartbeat_path.exists()
    content = heartbeat_path.read_text(encoding="utf-8")
    assert "# Heartbeat" in content
    assert "Review pending estimates" in content


async def test_heartbeat_text_round_trip_via_store() -> None:
    """Writing heartbeat_text via the store and reading it back should work."""
    store = get_user_store()
    user = await store.create(
        user_id="heartbeat-test",
        phone="+15551112222",
    )
    # Update with heartbeat text
    updated = await store.update(user.id, heartbeat_text="- [ ] Test item")
    assert updated is not None
    assert updated.heartbeat_text == "- [ ] Test item"

    # Re-read from disk
    reloaded = await store.get_by_id(user.id)
    assert reloaded is not None
    assert reloaded.heartbeat_text == "- [ ] Test item"

    # Verify the file on disk
    user_dir = Path(settings.data_dir) / str(user.id)
    heartbeat_path = user_dir / "HEARTBEAT.md"
    assert heartbeat_path.exists()
    content = heartbeat_path.read_text(encoding="utf-8")
    assert "# Heartbeat" in content
    assert "Test item" in content


async def test_default_heartbeat_seeded_on_create() -> None:
    """New users should get a default HEARTBEAT.md file."""
    store = get_user_store()
    user = await store.create(
        user_id="default-heartbeat-test",
        phone="+15559998888",
    )
    user_dir = Path(settings.data_dir) / str(user.id)
    heartbeat_path = user_dir / "HEARTBEAT.md"
    assert heartbeat_path.exists()
    content = heartbeat_path.read_text(encoding="utf-8")
    assert "# Heartbeat" in content
