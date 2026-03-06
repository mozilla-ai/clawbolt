from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import (
    ContractorData,
    SessionState,
    StoredMessage,
)
from backend.app.agent.memory import recall_memories, save_memory
from backend.app.agent.router import handle_inbound_message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response, make_tool_call_response


@pytest.fixture()
def session(test_contractor: ContractorData) -> SessionState:
    return SessionState(
        session_id="test-session",
        contractor_id=test_contractor.id,
        messages=[],
        is_active=True,
    )


@pytest.fixture()
def mock_messaging() -> MessagingService:
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="msg_42")
    service.send_media = AsyncMock(return_value="msg_43")
    service.send_message = AsyncMock(return_value="msg_42")
    service.send_typing_indicator = AsyncMock()
    service.download_media = AsyncMock()
    return service


@pytest.mark.asyncio()
async def test_recall_exact_match(test_contractor: ContractorData) -> None:
    """recall_facts should find exact keyword match."""
    await save_memory(
        test_contractor.id,
        key="johnson_deck_price",
        value="$4,500 for 12x12 composite deck",
        category="pricing",
    )
    results = await recall_memories(test_contractor.id, query="johnson_deck_price")
    assert len(results) == 1
    assert "4,500" in results[0].value


@pytest.mark.asyncio()
async def test_recall_keyword_search(test_contractor: ContractorData) -> None:
    """recall_memories should find by keyword in key or value."""
    await save_memory(
        test_contractor.id,
        key="smith_bathroom_quote",
        value="$3,200 for full bathroom remodel",
        category="pricing",
    )
    results = await recall_memories(test_contractor.id, query="bathroom")
    assert len(results) >= 1
    assert any("bathroom" in m.key or "bathroom" in m.value for m in results)


@pytest.mark.asyncio()
async def test_recall_no_results(test_contractor: ContractorData) -> None:
    """recall_memories should return empty list for unmatched query."""
    results = await recall_memories(test_contractor.id, query="nonexistent_xyz_query")
    assert results == []


@pytest.mark.asyncio()
async def test_recall_by_category(test_contractor: ContractorData) -> None:
    """recall_memories should filter by category."""
    await save_memory(test_contractor.id, key="deck_rate", value="$45/sqft", category="pricing")
    await save_memory(test_contractor.id, key="john_phone", value="555-1234", category="client")

    pricing_results = await recall_memories(test_contractor.id, query="deck", category="pricing")
    assert len(pricing_results) >= 1
    assert all(m.category == "pricing" for m in pricing_results)


@pytest.mark.asyncio()
async def test_recall_multiple_facts(test_contractor: ContractorData) -> None:
    """recall_memories should return multiple matching facts."""
    await save_memory(
        test_contractor.id,
        key="deck_rate",
        value="$45/sqft for decks",
        category="pricing",
    )
    await save_memory(
        test_contractor.id,
        key="deck_material",
        value="Prefers Trex composite for decks",
        category="general",
    )
    results = await recall_memories(test_contractor.id, query="deck")
    assert len(results) >= 2


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_recall_end_to_end_save_then_query(
    mock_amessages: object,
    test_contractor: ContractorData,
    session: SessionState,
    mock_messaging: MessagingService,
) -> None:
    """End-to-end: save a memory, then verify it's in context for next message."""
    # Step 1: Save a memory directly (simulating a previous conversation)
    await save_memory(
        test_contractor.id,
        key="johnson_deck",
        value="$4,500 for 12x12 composite deck",
        category="pricing",
    )

    # Step 2: Create a recall query message
    recall_msg = StoredMessage(
        direction="inbound",
        body="What did I quote for the Johnson deck?",
        seq=1,
    )
    session.messages.append(recall_msg)

    # Mock agent using recall_facts tool and returning answer
    tool_response = make_tool_call_response(
        [{"name": "recall_facts", "arguments": '{"query": "johnson deck"}', "id": "call_recall_0"}]
    )
    text_response = make_text_response("You quoted $4,500 for the Johnson 12x12 composite deck.")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    response = await handle_inbound_message(
        contractor=test_contractor,
        session=session,
        message=recall_msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "4,500" in response.reply_text
    assert any("recall_facts" in str(tc) for tc in response.tool_calls)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_system_prompt_includes_recall_guidance(
    mock_amessages: object,
    test_contractor: ContractorData,
    session: SessionState,
    mock_messaging: MessagingService,
) -> None:
    """System prompt should include recall behavior guidance."""
    msg = StoredMessage(
        direction="inbound",
        body="What do you know about my rates?",
        seq=1,
    )
    session.messages.append(msg)

    mock_amessages.return_value = make_text_response("Let me check my memory.")  # type: ignore[union-attr]

    await handle_inbound_message(
        contractor=test_contractor,
        session=session,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["system"]
    assert "Recall Behavior" in system_prompt
    assert "search your memory" in system_prompt.lower()
    assert "don't make things up" in system_prompt


@pytest.mark.asyncio()
async def test_build_memory_context_includes_saved_facts(
    test_contractor: ContractorData,
) -> None:
    """build_memory_context should include saved facts when query matches."""
    from backend.app.agent.memory import build_memory_context

    await save_memory(
        test_contractor.id,
        key="hourly_rate",
        value="$75/hour for general work",
        category="pricing",
    )

    # Direct keyword match on memory key
    context = await build_memory_context(test_contractor.id, query="hourly_rate")
    assert "$75/hour" in context

    # No query returns all memories
    context_all = await build_memory_context(test_contractor.id)
    assert "$75/hour" in context_all
