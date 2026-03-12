"""Tests for unified HEARTBEAT.md heartbeat in HeartbeatStore."""

from pathlib import Path

import pytest

from backend.app.agent.file_store import HeartbeatStore, UserData
from backend.app.config import settings


@pytest.mark.asyncio()
async def test_heartbeat_store_reads_heartbeat_md(test_user: UserData) -> None:
    """HeartbeatStore should read heartbeat items from HEARTBEAT.md."""
    store = HeartbeatStore(test_user.id)

    # The default HEARTBEAT.md is seeded on user creation with 3 default items.
    # Add two more to verify they are appended.
    await store.add_heartbeat_item("Task one", "daily")
    await store.add_heartbeat_item("Task two", "weekdays")

    items = await store.get_heartbeat_items()
    # 3 defaults + 2 added
    assert len(items) == 5
    descriptions = [i.description for i in items]
    assert "Task one" in descriptions
    assert "Task two" in descriptions

    # Verify items appear in HEARTBEAT.md on disk
    md_content = store.read_heartbeat_md()
    assert "- [ ] Task one" in md_content
    assert "- [ ] Task two (weekdays)" in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_update_marks_checked(test_user: UserData) -> None:
    """Updating status to completed should check the checkbox in HEARTBEAT.md."""
    store = HeartbeatStore(test_user.id)
    item = await store.add_heartbeat_item("Finish report")
    await store.update_heartbeat_item(item.id, status="completed")

    md_content = store.read_heartbeat_md()
    assert "- [x] Finish report" in md_content
    assert "- [ ] Finish report" not in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_delete_removes_line(test_user: UserData) -> None:
    """Deleting an item should remove its line from HEARTBEAT.md."""
    store = HeartbeatStore(test_user.id)
    await store.add_heartbeat_item("Keep this")
    item2 = await store.add_heartbeat_item("Remove this")

    deleted = await store.delete_heartbeat_item(item2.id)
    assert deleted is True

    md_content = store.read_heartbeat_md()
    assert "Keep this" in md_content
    assert "Remove this" not in md_content

    items = await store.get_heartbeat_items()
    # 3 default items + 1 remaining added item
    descriptions = [i.description for i in items]
    assert "Keep this" in descriptions
    assert "Remove this" not in descriptions


@pytest.mark.asyncio()
async def test_read_heartbeat_md_returns_empty_for_nonexistent_user() -> None:
    """read_heartbeat_md should return empty string when file does not exist."""
    # Use a user ID that has never been created (no HEARTBEAT.md on disk)
    store = HeartbeatStore(99999)
    assert store.read_heartbeat_md() == ""


@pytest.mark.asyncio()
async def test_parse_schedule_from_md(test_user: UserData) -> None:
    """Parser should extract schedule from parenthesized suffix."""
    user_dir = Path(settings.data_dir) / str(test_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    md_path = user_dir / "HEARTBEAT.md"
    md_path.write_text(
        "# Heartbeat\n\n"
        "- [ ] Daily task\n"
        "- [ ] Weekday task (weekdays)\n"
        "- [ ] One-time task (once)\n"
        "- [x] Done task\n",
        encoding="utf-8",
    )

    store = HeartbeatStore(test_user.id)
    items = await store.get_heartbeat_items()
    assert len(items) == 4
    assert items[0].description == "Daily task"
    assert items[0].schedule == "daily"
    assert items[0].status == "active"
    assert items[1].description == "Weekday task"
    assert items[1].schedule == "weekdays"
    assert items[2].description == "One-time task"
    assert items[2].schedule == "once"
    assert items[3].description == "Done task"
    assert items[3].status == "completed"
