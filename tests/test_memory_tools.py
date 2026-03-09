import pytest

from backend.app.agent.file_store import UserData
from backend.app.agent.tools.memory_tools import create_memory_tools


@pytest.mark.asyncio()
async def test_save_fact_tool(test_user: UserData) -> None:
    """save_fact tool should save a memory and return confirmation."""
    tools = create_memory_tools(test_user.id)
    save_fact = tools[0].function
    result = await save_fact(key="deck_rate", value="$35/sqft", category="pricing")
    assert "Saved" in result.content
    assert "deck_rate" in result.content
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_recall_facts_tool(test_user: UserData) -> None:
    """recall_facts tool should find saved memories."""
    tools = create_memory_tools(test_user.id)
    save_fact = tools[0].function
    recall_facts = tools[1].function

    await save_fact(key="deck_rate", value="$35/sqft")
    result = await recall_facts(query="deck")
    assert "deck_rate" in result.content
    assert "$35/sqft" in result.content


@pytest.mark.asyncio()
async def test_recall_facts_empty(test_user: UserData) -> None:
    """recall_facts should return message when no facts found."""
    tools = create_memory_tools(test_user.id)
    recall_facts = tools[1].function
    result = await recall_facts(query="nonexistent")
    assert "No matching facts" in result.content


@pytest.mark.asyncio()
async def test_forget_fact_tool(test_user: UserData) -> None:
    """forget_fact tool should delete a memory."""
    tools = create_memory_tools(test_user.id)
    save_fact = tools[0].function
    forget_fact = tools[2].function

    await save_fact(key="temp", value="temporary")
    result = await forget_fact(key="temp")
    assert "Deleted" in result.content
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_forget_fact_not_found(test_user: UserData) -> None:
    """forget_fact should handle missing keys with is_error=True."""
    tools = create_memory_tools(test_user.id)
    forget_fact = tools[2].function
    result = await forget_fact(key="nonexistent")
    assert "Not found" in result.content
    assert result.is_error is True
