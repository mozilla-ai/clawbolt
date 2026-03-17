"""Tests for unified HEARTBEAT.md heartbeat in HeartbeatStore."""

import pytest

from backend.app.agent.stores import HeartbeatStore
from backend.app.models import User


@pytest.mark.asyncio()
async def test_heartbeat_store_reads_heartbeat_md(test_user: User) -> None:
    """HeartbeatStore should read heartbeat items from HEARTBEAT.md."""
    store = HeartbeatStore(test_user.id)

    await store.add_heartbeat_item("Task one", "daily")
    await store.add_heartbeat_item("Task two", "weekdays")

    items = await store.get_heartbeat_items()
    assert len(items) == 2
    descriptions = [i.description for i in items]
    assert "Task one" in descriptions
    assert "Task two" in descriptions

    # Verify items appear in HEARTBEAT.md on disk
    md_content = store.read_heartbeat_md()
    assert "- [ ] Task one" in md_content
    assert "- [ ] Task two (weekdays)" in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_update_marks_checked(test_user: User) -> None:
    """Updating status to completed should check the checkbox in HEARTBEAT.md."""
    store = HeartbeatStore(test_user.id)
    item = await store.add_heartbeat_item("Finish report")
    await store.update_heartbeat_item(item.id, status="completed")

    md_content = store.read_heartbeat_md()
    assert "- [x] Finish report" in md_content
    assert "- [ ] Finish report" not in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_delete_removes_line(test_user: User) -> None:
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
    descriptions = [i.description for i in items]
    assert "Keep this" in descriptions
    assert "Remove this" not in descriptions


@pytest.mark.asyncio()
async def test_read_heartbeat_md_returns_empty_for_nonexistent_user() -> None:
    """read_heartbeat_md should return empty string when file does not exist."""
    # Use a user ID that has never been created (no HEARTBEAT.md on disk)
    store = HeartbeatStore("99999")
    assert store.read_heartbeat_md() == ""


@pytest.mark.asyncio()
async def test_schedule_and_status_roundtrip(test_user: User) -> None:
    """Items with different schedules and statuses should round-trip correctly."""
    store = HeartbeatStore(test_user.id)
    await store.add_heartbeat_item("Daily task", "daily")
    await store.add_heartbeat_item("Weekday task", "weekdays")
    await store.add_heartbeat_item("One-time task", "once")
    item4 = await store.add_heartbeat_item("Done task", "daily")
    await store.update_heartbeat_item(item4.id, status="completed")

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
