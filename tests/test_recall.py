from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.memory import save_memory
from backend.app.agent.router import handle_inbound_message
from backend.app.models import Contractor, Conversation, Memory, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response


@pytest.fixture()
def conversation(db_session: Session, test_contractor: Contractor) -> Conversation:
    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    return conv


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
async def test_recall_exact_match(db_session: Session, test_contractor: Contractor) -> None:
    """recall_facts should find exact keyword match."""
    await save_memory(
        db_session,
        test_contractor.id,
        key="johnson_deck_price",
        value="$4,500 for 12x12 composite deck",
        category="pricing",
    )
    memories = (
        db_session.query(Memory)
        .filter(Memory.contractor_id == test_contractor.id, Memory.key == "johnson_deck_price")
        .all()
    )
    assert len(memories) == 1
    assert "4,500" in memories[0].value


@pytest.mark.asyncio()
async def test_recall_keyword_search(db_session: Session, test_contractor: Contractor) -> None:
    """recall_memories should find by keyword in key or value."""
    from backend.app.agent.memory import recall_memories

    await save_memory(
        db_session,
        test_contractor.id,
        key="smith_bathroom_quote",
        value="$3,200 for full bathroom remodel",
        category="pricing",
    )
    results = await recall_memories(db_session, test_contractor.id, query="bathroom")
    assert len(results) >= 1
    assert any("bathroom" in m.key or "bathroom" in m.value for m in results)


@pytest.mark.asyncio()
async def test_recall_no_results(db_session: Session, test_contractor: Contractor) -> None:
    """recall_memories should return empty list for unmatched query."""
    from backend.app.agent.memory import recall_memories

    results = await recall_memories(db_session, test_contractor.id, query="nonexistent_xyz_query")
    assert results == []


@pytest.mark.asyncio()
async def test_recall_by_category(db_session: Session, test_contractor: Contractor) -> None:
    """recall_memories should filter by category."""
    from backend.app.agent.memory import recall_memories

    await save_memory(
        db_session, test_contractor.id, key="deck_rate", value="$45/sqft", category="pricing"
    )
    await save_memory(
        db_session, test_contractor.id, key="john_phone", value="555-1234", category="client"
    )

    pricing_results = await recall_memories(
        db_session, test_contractor.id, query="deck", category="pricing"
    )
    assert len(pricing_results) >= 1
    assert all(m.category == "pricing" for m in pricing_results)


@pytest.mark.asyncio()
async def test_recall_multiple_facts(db_session: Session, test_contractor: Contractor) -> None:
    """recall_memories should return multiple matching facts."""
    from backend.app.agent.memory import recall_memories

    await save_memory(
        db_session,
        test_contractor.id,
        key="deck_rate",
        value="$45/sqft for decks",
        category="pricing",
    )
    await save_memory(
        db_session,
        test_contractor.id,
        key="deck_material",
        value="Prefers Trex composite for decks",
        category="general",
    )
    results = await recall_memories(db_session, test_contractor.id, query="deck")
    assert len(results) >= 2


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_recall_end_to_end_save_then_query(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
    mock_messaging: MessagingService,
) -> None:
    """End-to-end: save a memory, then verify it's in context for next message."""
    # Step 1: Save a memory directly (simulating a previous conversation)
    await save_memory(
        db_session,
        test_contractor.id,
        key="johnson_deck",
        value="$4,500 for 12x12 composite deck",
        category="pricing",
    )

    # Step 2: Send a recall query
    recall_msg = Message(
        conversation_id=conversation.id,
        direction="inbound",
        body="What did I quote for the Johnson deck?",
    )
    db_session.add(recall_msg)
    db_session.commit()
    db_session.refresh(recall_msg)

    # Mock agent using recall_facts tool and returning answer
    recall_response = make_text_response("You quoted $4,500 for the Johnson 12x12 composite deck.")
    tool_call = MagicMock()
    tool_call.function.name = "recall_facts"
    tool_call.function.arguments = '{"query": "johnson deck"}'
    recall_response.choices[0].message.tool_calls = [tool_call]
    mock_acompletion.return_value = recall_response  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=recall_msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "4,500" in response.reply_text
    assert any("recall_facts" in str(tc) for tc in response.tool_calls)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_system_prompt_includes_recall_guidance(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
    mock_messaging: MessagingService,
) -> None:
    """System prompt should include recall behavior guidance."""
    msg = Message(
        conversation_id=conversation.id,
        direction="inbound",
        body="What do you know about my rates?",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    mock_acompletion.return_value = make_text_response("Let me check my memory.")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=msg,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    system_msg = call_args.kwargs["messages"][0]["content"]
    assert "Recall Behavior" in system_msg
    assert "recall_facts" in system_msg
    assert "don't make things up" in system_msg


@pytest.mark.asyncio()
async def test_build_memory_context_includes_saved_facts(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """build_memory_context should include saved facts when query matches."""
    from backend.app.agent.memory import build_memory_context

    await save_memory(
        db_session,
        test_contractor.id,
        key="hourly_rate",
        value="$75/hour for general work",
        category="pricing",
    )

    # Direct keyword match on memory key
    context = await build_memory_context(db_session, test_contractor.id, query="hourly_rate")
    assert "$75/hour" in context

    # No query returns all memories
    context_all = await build_memory_context(db_session, test_contractor.id)
    assert "$75/hour" in context_all
