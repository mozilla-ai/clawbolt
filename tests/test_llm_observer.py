"""Unit and integration tests for the LLM request observer hook."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from backend.app.agent.observer import (
    LLMRequestPayload,
    compute_min_message_seq,
    emit_llm_request,
    get_llm_request_observer,
    set_llm_request_observer,
)
from backend.app.models import User
from tests.mocks.llm import make_text_response


@pytest.fixture(autouse=True)
def _reset_observer() -> Iterator[None]:
    """Clear any registered observer between tests so module-level state does
    not leak across cases."""
    set_llm_request_observer(None)
    yield
    set_llm_request_observer(None)


def _make_payload() -> LLMRequestPayload:
    return LLMRequestPayload(
        schema_version=1,
        user_id="user-1",
        session_id="sess-1",
        request_id="req-1",
        model="claude-test",
        provider="anthropic",
        max_tokens=1024,
        thinking=None,
        system="hello",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        min_message_seq_in_prompt=42,
        started_at=datetime.now(UTC),
    )


def test_default_observer_is_none() -> None:
    assert get_llm_request_observer() is None


def test_set_and_get_observer() -> None:
    async def obs(_: LLMRequestPayload) -> None:
        return None

    set_llm_request_observer(obs)
    assert get_llm_request_observer() is obs

    set_llm_request_observer(None)
    assert get_llm_request_observer() is None


@pytest.mark.asyncio()
async def test_emit_calls_registered_observer() -> None:
    received: list[LLMRequestPayload] = []

    async def obs(payload: LLMRequestPayload) -> None:
        received.append(payload)

    set_llm_request_observer(obs)
    payload = _make_payload()
    await emit_llm_request(payload)

    assert received == [payload]


@pytest.mark.asyncio()
async def test_emit_is_noop_when_no_observer() -> None:
    # Should not raise.
    await emit_llm_request(_make_payload())


@pytest.mark.asyncio()
async def test_emit_swallows_observer_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(_: LLMRequestPayload) -> None:
        raise RuntimeError("observer blew up")

    set_llm_request_observer(boom)
    with caplog.at_level(logging.ERROR, logger="backend.app.agent.observer"):
        # Must not raise -- agent loop survives observer failure.
        await emit_llm_request(_make_payload())

    assert any("observer raised" in rec.message for rec in caplog.records)


def test_compute_min_message_seq_returns_lowest_persisted_seq() -> None:
    msgs = [
        SystemMessage(content="sys"),
        UserMessage(content="hi", seq=5),
        AssistantMessage(content="hello", seq=6),
        UserMessage(content="more", seq=7),
    ]
    assert compute_min_message_seq(msgs) == 5


def test_compute_min_message_seq_ignores_tool_results_and_system() -> None:
    msgs = [
        SystemMessage(content="sys"),
        UserMessage(content="hi", seq=10),
        AssistantMessage(content="ok", seq=11),
        ToolResultMessage(tool_call_id="t1", content="result"),
    ]
    # ToolResultMessage / SystemMessage should be ignored.
    assert compute_min_message_seq(msgs) == 10


def test_compute_min_message_seq_returns_none_when_no_persisted_seqs() -> None:
    msgs = [
        UserMessage(content="hi"),  # seq defaults to None
        AssistantMessage(content="hello"),  # seq defaults to None
    ]
    assert compute_min_message_seq(msgs) is None


def test_compute_min_message_seq_skips_none_seqs() -> None:
    msgs = [
        UserMessage(content="hi"),  # seq=None -> skipped
        UserMessage(content="next", seq=3),
        AssistantMessage(content="resp", seq=4),
    ]
    assert compute_min_message_seq(msgs) == 3


# ---------------------------------------------------------------------------
# Integration: observer fires from inside _call_llm_with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_observer_fires_from_agent_loop(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """Registering an observer causes it to be invoked with each LLM request
    dispatched by the agent loop, with the user/session identifiers from the
    surrounding agent populated."""
    mock_amessages.return_value = make_text_response("ok")

    received: list[LLMRequestPayload] = []

    async def obs(payload: LLMRequestPayload) -> None:
        received.append(payload)

    set_llm_request_observer(obs)
    try:
        agent = ClawboltAgent(user=test_user, session_id="sess-int", request_id="req-int")
        await agent.process_message("hello there")
    finally:
        set_llm_request_observer(None)

    assert received, "observer was not invoked"
    payload = received[0]
    assert payload.schema_version == 1
    assert payload.user_id == test_user.id
    assert payload.session_id == "sess-int"
    assert payload.request_id == "req-int"
    assert payload.messages, "messages should be non-empty when LLM is dispatched"
    assert isinstance(payload.started_at, datetime)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_observer_exception_does_not_crash_agent_loop(
    mock_amessages: MagicMock,
    test_user: User,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An observer that raises must be caught: the agent loop continues and
    ``process_message`` succeeds."""
    mock_amessages.return_value = make_text_response("ok")

    async def boom(_: LLMRequestPayload) -> None:
        raise RuntimeError("observer blew up")

    set_llm_request_observer(boom)
    try:
        with caplog.at_level(logging.ERROR, logger="backend.app.agent.observer"):
            agent = ClawboltAgent(user=test_user)
            # Must not raise.
            await agent.process_message("still ok")
    finally:
        set_llm_request_observer(None)

    assert any("observer raised" in rec.message for rec in caplog.records)
