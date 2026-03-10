"""Tests for heartbeat checklist management tools."""

import pytest

from backend.app.agent.file_store import HeartbeatStore, UserData
from backend.app.agent.tools.checklist_tools import create_checklist_tools


@pytest.mark.asyncio()
async def test_add_checklist_item(test_user: UserData) -> None:
    """add_checklist_item tool should create item and return confirmation."""
    tools = create_checklist_tools(test_user.id)
    add_item = tools[0].function
    result = await add_item(description="Check material prices", schedule="daily")
    assert "Added to checklist" in result.content
    assert "material prices" in result.content
    assert "daily" in result.content
    assert result.is_error is False

    # Verify in store
    store = HeartbeatStore(test_user.id)
    items = await store.get_checklist()
    active = [i for i in items if i.status == "active"]
    # Default CHECKLIST.md has 3 items + 1 added
    assert len(active) >= 1
    added = [i for i in active if i.description == "Check material prices"]
    assert len(added) == 1
    assert added[0].schedule == "daily"
    assert added[0].status == "active"


@pytest.mark.asyncio()
async def test_add_checklist_item_default_schedule(
    test_user: UserData,
) -> None:
    """add_checklist_item should default to daily schedule."""
    tools = create_checklist_tools(test_user.id)
    add_item = tools[0].function
    await add_item(description="Morning check")

    store = HeartbeatStore(test_user.id)
    items = await store.get_checklist()
    active = [i for i in items if i.status == "active"]
    added = [i for i in active if i.description == "Morning check"]
    assert len(added) == 1
    assert added[0].schedule == "daily"


@pytest.mark.asyncio()
async def test_add_checklist_item_invalid_schedule(
    test_user: UserData,
) -> None:
    """add_checklist_item should reject invalid schedule values."""
    tools = create_checklist_tools(test_user.id)
    add_item = tools[0].function
    result = await add_item(description="Bad schedule", schedule="hourly")
    assert "Invalid schedule" in result.content
    assert result.is_error is True

    store = HeartbeatStore(test_user.id)
    items = await store.get_checklist()
    # No "Bad schedule" item should have been added
    bad = [i for i in items if i.description == "Bad schedule"]
    assert len(bad) == 0


@pytest.mark.asyncio()
async def test_list_checklist_items(test_user: UserData) -> None:
    """list_checklist_items should show active items."""
    tools = create_checklist_tools(test_user.id)
    add_item = tools[0].function
    list_items = tools[1].function

    await add_item(description="Check inbox")
    await add_item(description="Review quotes", schedule="weekdays")

    result = await list_items()
    assert "Check inbox" in result.content
    assert "Review quotes" in result.content
    assert "daily" in result.content
    assert "weekdays" in result.content


@pytest.mark.asyncio()
async def test_list_checklist_items_with_defaults(test_user: UserData) -> None:
    """list_checklist_items should include default CHECKLIST.md items."""
    tools = create_checklist_tools(test_user.id)
    list_items = tools[1].function
    result = await list_items()
    # Default CHECKLIST.md has seeded items
    assert "Follow up with new leads" in result.content


@pytest.mark.asyncio()
async def test_list_excludes_completed(test_user: UserData) -> None:
    """list_checklist_items should not show completed items."""
    store = HeartbeatStore(test_user.id)
    item = await store.add_checklist_item(description="Done item", schedule="daily")
    await store.update_checklist_item(item.id, status="completed")

    tools = create_checklist_tools(test_user.id)
    list_items = tools[1].function
    result = await list_items()
    # The completed item should not appear in the listing
    assert "Done item" not in result.content


@pytest.mark.asyncio()
async def test_remove_checklist_item(test_user: UserData) -> None:
    """remove_checklist_item should delete item and return confirmation."""
    tools = create_checklist_tools(test_user.id)
    add_item = tools[0].function
    remove_item = tools[2].function

    await add_item(description="To remove")

    store = HeartbeatStore(test_user.id)
    items = await store.get_checklist()
    added = [i for i in items if i.description == "To remove"]
    assert len(added) == 1
    item_id = added[0].id

    result = await remove_item(item_id=item_id)
    assert "Removed" in result.content
    assert result.is_error is False

    items = await store.get_checklist()
    removed = [i for i in items if i.description == "To remove"]
    assert len(removed) == 0


@pytest.mark.asyncio()
async def test_remove_checklist_item_not_found(
    test_user: UserData,
) -> None:
    """remove_checklist_item should handle missing IDs."""
    tools = create_checklist_tools(test_user.id)
    remove_item = tools[2].function
    result = await remove_item(item_id=999)
    assert "not found" in result.content
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_remove_scoped_to_user(
    test_user: UserData,
) -> None:
    """remove_checklist_item should not delete another user's items.

    Each user's CHECKLIST.md is a separate file, so IDs are per-user.
    Attempting to remove an ID that does not exist in the current user's
    checklist should return not-found.
    """
    other_store = HeartbeatStore(99)
    await other_store.add_checklist_item(description="Other's item", schedule="daily")
    other_items = await other_store.get_checklist()
    assert len(other_items) == 1

    # Use an ID that definitely does not exist in test_user's CHECKLIST.md
    tools = create_checklist_tools(test_user.id)
    remove_item = tools[2].function
    result = await remove_item(item_id=9999)
    assert "not found" in result.content
    assert result.is_error is True

    # Item should still exist in other user's store
    remaining = await other_store.get_checklist()
    other_items = [i for i in remaining if i.description == "Other's item"]
    assert len(other_items) == 1
