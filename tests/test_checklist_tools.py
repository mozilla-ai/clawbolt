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
    assert len(active) == 1
    assert active[0].description == "Check material prices"
    assert active[0].schedule == "daily"
    assert active[0].status == "active"


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
    assert len(active) == 1
    assert active[0].schedule == "daily"


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
    assert len(items) == 0


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
async def test_list_checklist_items_empty(test_user: UserData) -> None:
    """list_checklist_items should return message when empty."""
    tools = create_checklist_tools(test_user.id)
    list_items = tools[1].function
    result = await list_items()
    assert "No active checklist items" in result.content


@pytest.mark.asyncio()
async def test_list_excludes_paused(test_user: UserData) -> None:
    """list_checklist_items should not show paused items."""
    store = HeartbeatStore(test_user.id)
    await store.add_checklist_item(description="Paused item", schedule="daily")
    # Mark it as paused
    items = await store.get_checklist()
    await store.update_checklist_item(items[0].id, status="paused")

    tools = create_checklist_tools(test_user.id)
    list_items = tools[1].function
    result = await list_items()
    assert "No active checklist items" in result.content


@pytest.mark.asyncio()
async def test_remove_checklist_item(test_user: UserData) -> None:
    """remove_checklist_item should delete item and return confirmation."""
    tools = create_checklist_tools(test_user.id)
    add_item = tools[0].function
    remove_item = tools[2].function

    await add_item(description="To remove")

    store = HeartbeatStore(test_user.id)
    items = await store.get_checklist()
    active = [i for i in items if i.status == "active"]
    assert len(active) == 1
    item_id = active[0].id

    result = await remove_item(item_id=item_id)
    assert "Removed" in result.content
    assert result.is_error is False

    items = await store.get_checklist()
    assert len(items) == 0


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
    """remove_checklist_item should not delete another user's items."""
    other_store = HeartbeatStore(99)
    await other_store.add_checklist_item(description="Other's item", schedule="daily")
    other_items = await other_store.get_checklist()
    assert len(other_items) == 1

    tools = create_checklist_tools(test_user.id)
    remove_item = tools[2].function
    result = await remove_item(item_id=other_items[0].id)
    assert "not found" in result.content
    assert result.is_error is True

    # Item should still exist in other user's store
    remaining = await other_store.get_checklist()
    assert len(remaining) == 1
