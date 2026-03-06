import pytest

from backend.app.agent.file_store import ClientStore, ContractorData
from backend.app.agent.memory import (
    build_memory_context,
    delete_memory,
    get_all_memories,
    recall_memories,
    save_memory,
)


@pytest.mark.asyncio()
async def test_save_memory_creates_new(test_contractor: ContractorData) -> None:
    """save_memory should create a new memory entry."""
    memory = await save_memory(
        test_contractor.id,
        key="deck_pricing",
        value="$35-45/sqft for composite",
        category="pricing",
    )
    assert memory.key == "deck_pricing"
    assert memory.value == "$35-45/sqft for composite"
    assert memory.category == "pricing"


@pytest.mark.asyncio()
async def test_save_memory_updates_existing(test_contractor: ContractorData) -> None:
    """save_memory with same key should update the value."""
    await save_memory(test_contractor.id, key="rate", value="$50/hr")
    updated = await save_memory(test_contractor.id, key="rate", value="$55/hr")
    assert updated.value == "$55/hr"

    all_memories = await get_all_memories(test_contractor.id)
    assert len(all_memories) == 1


@pytest.mark.asyncio()
async def test_recall_memories_keyword_search(test_contractor: ContractorData) -> None:
    """recall_memories should find memories by keyword match."""
    await save_memory(test_contractor.id, key="deck_pricing", value="$35/sqft")
    await save_memory(test_contractor.id, key="fence_pricing", value="$20/ft")
    await save_memory(test_contractor.id, key="supplier", value="ABC Lumber")

    results = await recall_memories(test_contractor.id, query="pricing")
    assert len(results) == 2


@pytest.mark.asyncio()
async def test_recall_memories_by_category(test_contractor: ContractorData) -> None:
    """recall_memories with category filter should narrow results."""
    await save_memory(test_contractor.id, key="deck", value="deck work", category="pricing")
    await save_memory(test_contractor.id, key="client_deck", value="deck client", category="client")

    results = await recall_memories(test_contractor.id, query="deck", category="pricing")
    assert len(results) == 1
    assert results[0].category == "pricing"


@pytest.mark.asyncio()
async def test_get_all_memories(test_contractor: ContractorData) -> None:
    """get_all_memories should return all memories for a contractor."""
    await save_memory(test_contractor.id, key="a", value="1")
    await save_memory(test_contractor.id, key="b", value="2")

    all_mems = await get_all_memories(test_contractor.id)
    assert len(all_mems) == 2


@pytest.mark.asyncio()
async def test_delete_memory(test_contractor: ContractorData) -> None:
    """delete_memory should remove the memory."""
    await save_memory(test_contractor.id, key="temp", value="temporary")
    result = await delete_memory(test_contractor.id, key="temp")
    assert result is True

    all_mems = await get_all_memories(test_contractor.id)
    assert len(all_mems) == 0


@pytest.mark.asyncio()
async def test_delete_memory_not_found(test_contractor: ContractorData) -> None:
    """delete_memory should return False if key not found."""
    result = await delete_memory(test_contractor.id, key="nonexistent")
    assert result is False


@pytest.mark.asyncio()
async def test_build_memory_context(test_contractor: ContractorData) -> None:
    """build_memory_context should produce formatted text."""
    await save_memory(
        test_contractor.id,
        key="deck_pricing",
        value="$35/sqft",
        category="pricing",
        confidence=0.9,
    )
    client_store = ClientStore(test_contractor.id)
    await client_store.create(
        name="John Smith",
        phone="555-1234",
        address="123 Oak St",
    )

    context = await build_memory_context(test_contractor.id)
    assert "deck_pricing" in context
    assert "$35/sqft" in context
    assert "John Smith" in context
    assert "123 Oak St" in context
