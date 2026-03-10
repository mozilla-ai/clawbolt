"""Tests for checklist.json to CHECKLIST.md migration and unified checklist."""

import json
from pathlib import Path

import pytest

from backend.app.agent.file_store import HeartbeatStore, UserData
from backend.app.config import settings


@pytest.mark.asyncio()
async def test_migrate_json_to_md(test_user: UserData) -> None:
    """Legacy checklist.json items should be migrated into CHECKLIST.md."""
    user_dir = Path(settings.data_dir) / str(test_user.id)
    hb_dir = user_dir / "heartbeat"
    hb_dir.mkdir(parents=True, exist_ok=True)

    # Write a legacy checklist.json
    legacy_items = [
        {
            "id": 1,
            "user_id": test_user.id,
            "description": "Follow up with leads",
            "schedule": "daily",
            "status": "active",
        },
        {
            "id": 2,
            "user_id": test_user.id,
            "description": "Weekly review",
            "schedule": "weekdays",
            "status": "active",
        },
        {
            "id": 3,
            "user_id": test_user.id,
            "description": "Old completed",
            "schedule": "once",
            "status": "completed",
        },
    ]
    legacy_path = hb_dir / "checklist.json"
    legacy_path.write_text(json.dumps(legacy_items), encoding="utf-8")

    store = HeartbeatStore(test_user.id)
    migrated = await store.migrate_json_to_md()
    assert migrated is True

    # Legacy file should be renamed
    assert not legacy_path.exists()
    assert (hb_dir / "checklist.json.migrated").exists()

    # CHECKLIST.md should contain the active items
    md_content = store.read_checklist_md()
    assert "Follow up with leads" in md_content
    assert "Weekly review (weekdays)" in md_content
    # Completed items should not be migrated
    assert "Old completed" not in md_content


@pytest.mark.asyncio()
async def test_migrate_no_json(test_user: UserData) -> None:
    """Migration should return False when no legacy file exists."""
    store = HeartbeatStore(test_user.id)
    result = await store.migrate_json_to_md()
    assert result is False


@pytest.mark.asyncio()
async def test_migrate_empty_json(test_user: UserData) -> None:
    """Migration should handle empty checklist.json gracefully."""
    user_dir = Path(settings.data_dir) / str(test_user.id)
    hb_dir = user_dir / "heartbeat"
    hb_dir.mkdir(parents=True, exist_ok=True)

    legacy_path = hb_dir / "checklist.json"
    legacy_path.write_text("[]", encoding="utf-8")

    store = HeartbeatStore(test_user.id)
    result = await store.migrate_json_to_md()
    assert result is False
    assert not legacy_path.exists()
    assert (hb_dir / "checklist.json.migrated").exists()


@pytest.mark.asyncio()
async def test_migrate_appends_to_existing_md(test_user: UserData) -> None:
    """Migration should append to existing CHECKLIST.md content."""
    user_dir = Path(settings.data_dir) / str(test_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    hb_dir = user_dir / "heartbeat"
    hb_dir.mkdir(parents=True, exist_ok=True)

    # Write existing CHECKLIST.md
    md_path = user_dir / "CHECKLIST.md"
    md_path.write_text("# Checklist\n\n- [ ] Existing task\n", encoding="utf-8")

    # Write legacy JSON
    legacy_items = [
        {
            "id": 1,
            "user_id": test_user.id,
            "description": "Migrated task",
            "schedule": "daily",
            "status": "active",
        },
    ]
    legacy_path = hb_dir / "checklist.json"
    legacy_path.write_text(json.dumps(legacy_items), encoding="utf-8")

    store = HeartbeatStore(test_user.id)
    migrated = await store.migrate_json_to_md()
    assert migrated is True

    md_content = store.read_checklist_md()
    assert "Existing task" in md_content
    assert "Migrated task" in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_reads_checklist_md(test_user: UserData) -> None:
    """HeartbeatStore should read checklist items from CHECKLIST.md."""
    store = HeartbeatStore(test_user.id)

    # The default CHECKLIST.md is seeded on user creation with 3 default items.
    # Add two more to verify they are appended.
    await store.add_checklist_item("Task one", "daily")
    await store.add_checklist_item("Task two", "weekdays")

    items = await store.get_checklist()
    # 3 defaults + 2 added
    assert len(items) == 5
    descriptions = [i.description for i in items]
    assert "Task one" in descriptions
    assert "Task two" in descriptions

    # Verify items appear in CHECKLIST.md on disk
    md_content = store.read_checklist_md()
    assert "- [ ] Task one" in md_content
    assert "- [ ] Task two (weekdays)" in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_update_marks_checked(test_user: UserData) -> None:
    """Updating status to completed should check the checkbox in CHECKLIST.md."""
    store = HeartbeatStore(test_user.id)
    item = await store.add_checklist_item("Finish report")
    await store.update_checklist_item(item.id, status="completed")

    md_content = store.read_checklist_md()
    assert "- [x] Finish report" in md_content
    assert "- [ ] Finish report" not in md_content


@pytest.mark.asyncio()
async def test_heartbeat_store_delete_removes_line(test_user: UserData) -> None:
    """Deleting an item should remove its line from CHECKLIST.md."""
    store = HeartbeatStore(test_user.id)
    await store.add_checklist_item("Keep this")
    item2 = await store.add_checklist_item("Remove this")

    deleted = await store.delete_checklist_item(item2.id)
    assert deleted is True

    md_content = store.read_checklist_md()
    assert "Keep this" in md_content
    assert "Remove this" not in md_content

    items = await store.get_checklist()
    # 3 default items + 1 remaining added item
    descriptions = [i.description for i in items]
    assert "Keep this" in descriptions
    assert "Remove this" not in descriptions


@pytest.mark.asyncio()
async def test_read_checklist_md_returns_empty_for_nonexistent_user() -> None:
    """read_checklist_md should return empty string when file does not exist."""
    # Use a user ID that has never been created (no CHECKLIST.md on disk)
    store = HeartbeatStore(99999)
    assert store.read_checklist_md() == ""


@pytest.mark.asyncio()
async def test_parse_schedule_from_md(test_user: UserData) -> None:
    """Parser should extract schedule from parenthesized suffix."""
    user_dir = Path(settings.data_dir) / str(test_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    md_path = user_dir / "CHECKLIST.md"
    md_path.write_text(
        "# Checklist\n\n"
        "- [ ] Daily task\n"
        "- [ ] Weekday task (weekdays)\n"
        "- [ ] One-time task (once)\n"
        "- [x] Done task\n",
        encoding="utf-8",
    )

    store = HeartbeatStore(test_user.id)
    items = await store.get_checklist()
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
