"""Tests for checklist_text field via the profile endpoint (HEARTBEAT.md)."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.agent.file_store import get_user_store
from backend.app.config import settings


def test_profile_includes_checklist_text(client: TestClient) -> None:
    """Profile response should include the checklist_text field."""
    resp = client.get("/api/user/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert "checklist_text" in data


def test_update_checklist_text(client: TestClient) -> None:
    """Saving checklist_text via profile update should persist it."""
    checklist = "- [ ] Follow up with leads\n- [ ] Check job site"
    resp = client.put(
        "/api/user/profile",
        json={"checklist_text": checklist},
    )
    assert resp.status_code == 200
    assert resp.json()["checklist_text"] == checklist


def test_checklist_text_writes_checklist_md(client: TestClient) -> None:
    """Updating checklist_text should create a HEARTBEAT.md file on disk."""
    checklist = "- [ ] Review pending estimates"
    resp = client.put("/api/user/profile", json={"checklist_text": checklist})
    assert resp.status_code == 200

    # Find the user directory (user id=1 is the test user)
    user_dir = Path(settings.data_dir) / "1"
    checklist_path = user_dir / "HEARTBEAT.md"
    assert checklist_path.exists()
    content = checklist_path.read_text(encoding="utf-8")
    assert "# Checklist" in content
    assert "Review pending estimates" in content


async def test_checklist_text_round_trip_via_store() -> None:
    """Writing checklist_text via the store and reading it back should work."""
    store = get_user_store()
    user = await store.create(
        user_id="checklist-test",
        phone="+15551112222",
    )
    # Update with checklist text
    updated = await store.update(user.id, checklist_text="- [ ] Test item")
    assert updated is not None
    assert updated.checklist_text == "- [ ] Test item"

    # Re-read from disk
    reloaded = await store.get_by_id(user.id)
    assert reloaded is not None
    assert reloaded.checklist_text == "- [ ] Test item"

    # Verify the file on disk
    user_dir = Path(settings.data_dir) / str(user.id)
    checklist_path = user_dir / "HEARTBEAT.md"
    assert checklist_path.exists()
    content = checklist_path.read_text(encoding="utf-8")
    assert "# Checklist" in content
    assert "Test item" in content


async def test_default_checklist_seeded_on_create() -> None:
    """New users should get a default HEARTBEAT.md file."""
    store = get_user_store()
    user = await store.create(
        user_id="default-checklist-test",
        phone="+15559998888",
    )
    user_dir = Path(settings.data_dir) / str(user.id)
    checklist_path = user_dir / "HEARTBEAT.md"
    assert checklist_path.exists()
    content = checklist_path.read_text(encoding="utf-8")
    assert "# Checklist" in content
