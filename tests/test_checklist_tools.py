"""Tests for heartbeat checklist management tools."""

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.tools.checklist_tools import create_checklist_tools
from backend.app.models import Contractor, HeartbeatChecklistItem


@pytest.mark.asyncio()
async def test_add_checklist_item(db_session: Session, test_contractor: Contractor) -> None:
    """add_checklist_item tool should create item and return confirmation."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    add_item = tools[0].function
    result = await add_item(description="Check material prices", schedule="daily")
    assert "Added to checklist" in result
    assert "material prices" in result
    assert "daily" in result

    # Verify in DB
    items = db_session.query(HeartbeatChecklistItem).all()
    assert len(items) == 1
    assert items[0].description == "Check material prices"
    assert items[0].schedule == "daily"
    assert items[0].status == "active"


@pytest.mark.asyncio()
async def test_add_checklist_item_default_schedule(
    db_session: Session, test_contractor: Contractor
) -> None:
    """add_checklist_item should default to daily schedule."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    add_item = tools[0].function
    await add_item(description="Morning check")

    item = db_session.query(HeartbeatChecklistItem).first()
    assert item is not None
    assert item.schedule == "daily"


@pytest.mark.asyncio()
async def test_add_checklist_item_invalid_schedule(
    db_session: Session, test_contractor: Contractor
) -> None:
    """add_checklist_item should reject invalid schedule values."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    add_item = tools[0].function
    result = await add_item(description="Bad schedule", schedule="hourly")
    assert "Invalid schedule" in result

    items = db_session.query(HeartbeatChecklistItem).all()
    assert len(items) == 0


@pytest.mark.asyncio()
async def test_list_checklist_items(db_session: Session, test_contractor: Contractor) -> None:
    """list_checklist_items should show active items."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    add_item = tools[0].function
    list_items = tools[1].function

    await add_item(description="Check inbox")
    await add_item(description="Review quotes", schedule="weekdays")

    result = await list_items()
    assert "Check inbox" in result
    assert "Review quotes" in result
    assert "daily" in result
    assert "weekdays" in result


@pytest.mark.asyncio()
async def test_list_checklist_items_empty(db_session: Session, test_contractor: Contractor) -> None:
    """list_checklist_items should return message when empty."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    list_items = tools[1].function
    result = await list_items()
    assert "No active checklist items" in result


@pytest.mark.asyncio()
async def test_list_excludes_paused(db_session: Session, test_contractor: Contractor) -> None:
    """list_checklist_items should not show paused items."""
    item = HeartbeatChecklistItem(
        contractor_id=test_contractor.id,
        description="Paused item",
        schedule="daily",
        status="paused",
    )
    db_session.add(item)
    db_session.commit()

    tools = create_checklist_tools(db_session, test_contractor.id)
    list_items = tools[1].function
    result = await list_items()
    assert "No active checklist items" in result


@pytest.mark.asyncio()
async def test_remove_checklist_item(db_session: Session, test_contractor: Contractor) -> None:
    """remove_checklist_item should delete item and return confirmation."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    add_item = tools[0].function
    remove_item = tools[2].function

    await add_item(description="To remove")

    item = db_session.query(HeartbeatChecklistItem).first()
    assert item is not None

    result = await remove_item(item_id=item.id)
    assert "Removed" in result
    assert "To remove" in result

    items = db_session.query(HeartbeatChecklistItem).all()
    assert len(items) == 0


@pytest.mark.asyncio()
async def test_remove_checklist_item_not_found(
    db_session: Session, test_contractor: Contractor
) -> None:
    """remove_checklist_item should handle missing IDs."""
    tools = create_checklist_tools(db_session, test_contractor.id)
    remove_item = tools[2].function
    result = await remove_item(item_id=999)
    assert "not found" in result


@pytest.mark.asyncio()
async def test_remove_scoped_to_contractor(
    db_session: Session, test_contractor: Contractor
) -> None:
    """remove_checklist_item should not delete another contractor's items."""
    other = Contractor(user_id="other-user", phone="+15550000000")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    item = HeartbeatChecklistItem(
        contractor_id=other.id,
        description="Other's item",
        schedule="daily",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    tools = create_checklist_tools(db_session, test_contractor.id)
    remove_item = tools[2].function
    result = await remove_item(item_id=item.id)
    assert "not found" in result

    # Item should still exist
    remaining = db_session.query(HeartbeatChecklistItem).all()
    assert len(remaining) == 1
