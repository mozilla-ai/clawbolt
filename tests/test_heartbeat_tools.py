"""Tests for heartbeat management tools."""

import pytest

import backend.app.database as _db_module
from backend.app.agent.file_store import HeartbeatStore
from backend.app.agent.tools.heartbeat_tools import create_heartbeat_tools
from backend.app.models import User


@pytest.mark.asyncio()
async def test_get_heartbeat_returns_text(test_user: User) -> None:
    """get_heartbeat should return the user's heartbeat_text."""
    # Seed heartbeat text directly
    store = HeartbeatStore(test_user.id)
    await store.write_heartbeat_md("- Check material prices\n- Follow up with client")

    tools = create_heartbeat_tools(test_user.id)
    get_hb = tools[0].function
    result = await get_hb()
    assert "Check material prices" in result.content
    assert "Follow up with client" in result.content
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_get_heartbeat_empty(test_user: User) -> None:
    """get_heartbeat should return a friendly message when no text is set."""
    tools = create_heartbeat_tools(test_user.id)
    get_hb = tools[0].function
    result = await get_hb()
    assert "No heartbeat notes set" in result.content
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_update_heartbeat_writes_text(test_user: User) -> None:
    """update_heartbeat should write new text to heartbeat_text."""
    tools = create_heartbeat_tools(test_user.id)
    update_hb = tools[1].function
    result = await update_hb(text="- Daily site check\n- Review inbox")
    assert "updated" in result.content.lower()
    assert result.is_error is False

    # Verify via store
    store = HeartbeatStore(test_user.id)
    text = store.read_heartbeat_md()
    assert "Daily site check" in text
    assert "Review inbox" in text


@pytest.mark.asyncio()
async def test_update_heartbeat_clears_with_empty_text(test_user: User) -> None:
    """update_heartbeat with empty text should clear heartbeat_text."""
    store = HeartbeatStore(test_user.id)
    await store.write_heartbeat_md("- Old reminder")

    tools = create_heartbeat_tools(test_user.id)
    update_hb = tools[1].function
    result = await update_hb(text="")
    assert "updated" in result.content.lower()

    text = store.read_heartbeat_md()
    assert text == ""


@pytest.mark.asyncio()
async def test_update_then_get_roundtrip(test_user: User) -> None:
    """Writing and reading heartbeat text should round-trip correctly."""
    tools = create_heartbeat_tools(test_user.id)
    update_hb = tools[1].function
    get_hb = tools[0].function

    new_text = "## Morning\n- Check voicemail\n- Review schedule"
    await update_hb(text=new_text)

    result = await get_hb()
    assert result.content == new_text


@pytest.mark.asyncio()
async def test_update_heartbeat_shows_previous_content(test_user: User) -> None:
    """update_heartbeat should include previous content in the result (#873)."""
    store = HeartbeatStore(test_user.id)
    await store.write_heartbeat_md("- Old reminder\n- Follow up with client")

    tools = create_heartbeat_tools(test_user.id)
    update_hb = tools[1].function
    result = await update_hb(text="- New item only")
    assert "updated" in result.content.lower()
    assert "Previous content:" in result.content
    assert "Old reminder" in result.content
    assert "Follow up with client" in result.content


@pytest.mark.asyncio()
async def test_update_heartbeat_empty_previous(test_user: User) -> None:
    """update_heartbeat on an empty file should note it was empty (#873)."""
    tools = create_heartbeat_tools(test_user.id)
    update_hb = tools[1].function
    result = await update_hb(text="- First item")
    assert "was empty" in result.content


@pytest.mark.asyncio()
async def test_update_heartbeat_description_warns_about_overwrite(test_user: User) -> None:
    """update_heartbeat tool description should warn about overwrite behavior (#873)."""
    tools = create_heartbeat_tools(test_user.id)
    update_tool = tools[1]
    desc = update_tool.description.lower()
    assert "overwrite" in desc or "overwrites" in desc
    assert "never re-add" in desc or "do not restore" in desc


@pytest.mark.asyncio()
async def test_heartbeat_scoped_to_user(test_user: User) -> None:
    """Each user's heartbeat text is independent."""
    db = _db_module.SessionLocal()
    try:
        other_user = User(user_id="hb-other-99", phone="+15559999999")
        db.add(other_user)
        db.commit()
        db.refresh(other_user)
        other_id = other_user.id
        db.expunge(other_user)
    finally:
        db.close()

    # Set heartbeat for both users
    tools_a = create_heartbeat_tools(test_user.id)
    tools_b = create_heartbeat_tools(other_id)
    await tools_a[1].function(text="User A notes")
    await tools_b[1].function(text="User B notes")

    result_a = await tools_a[0].function()
    result_b = await tools_b[0].function()

    assert "User A notes" in result_a.content
    assert "User B notes" not in result_a.content
    assert "User B notes" in result_b.content
    assert "User A notes" not in result_b.content
