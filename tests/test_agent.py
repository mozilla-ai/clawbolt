from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.models import Contractor
from tests.mocks.llm import make_text_response


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_responds_to_message(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should produce a reply from LLM response."""
    mock_acompletion.return_value = make_text_response("Sure, I can help with that deck estimate!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    response = await agent.process_message("I need a quote for a 12x12 composite deck")

    assert response.reply_text == "Sure, I can help with that deck estimate!"
    mock_acompletion.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_includes_conversation_history(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should include conversation history in LLM call."""
    mock_acompletion.return_value = make_text_response("Got it!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    history = [
        {"role": "user", "content": "Hi, I need help"},
        {"role": "assistant", "content": "Hello! How can I help?"},
    ]
    await agent.process_message("What about a deck?", conversation_history=history)

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    # system + 2 history + 1 current = 4
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "Hi, I need help"
    assert messages[3]["content"] == "What about a deck?"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_system_prompt_includes_soul(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent system prompt should include contractor profile info."""
    mock_acompletion.return_value = make_text_response("Ok!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message("Hello")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["messages"][0]["content"]
    assert test_contractor.name in system_prompt
