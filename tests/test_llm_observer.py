"""Unit and integration tests for the LLM request observer hook."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from any_llm import ContextLengthExceededError

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from backend.app.agent.observer import (
    PURPOSE_AGENT_FOLLOWUP,
    PURPOSE_AGENT_MAIN,
    LLMRequestPayload,
    LLMResponsePayload,
    compute_min_message_seq,
    emit_llm_request,
    emit_llm_response,
    get_llm_request_observer,
    get_llm_response_observer,
    set_llm_request_observer,
    set_llm_response_observer,
)
from backend.app.models import User
from tests.mocks.llm import make_text_response


@pytest.fixture(autouse=True)
def _reset_observer() -> Iterator[None]:
    """Clear any registered observer between tests so module-level state does
    not leak across cases."""
    set_llm_request_observer(None)
    set_llm_response_observer(None)
    yield
    set_llm_request_observer(None)
    set_llm_response_observer(None)


def _make_payload() -> LLMRequestPayload:
    return LLMRequestPayload(
        schema_version=1,
        purpose=PURPOSE_AGENT_MAIN,
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


def test_payload_schema_version_is_one() -> None:
    """Pin the schema version. Bumping it is a coordinated change with
    every observer implementation; this guard keeps the bump deliberate."""
    assert _make_payload().schema_version == 1


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
    # And the broken observer is still registered: a transient failure
    # does not deregister the callback, so subsequent payloads continue
    # to fire and (potentially) succeed.
    assert get_llm_request_observer() is boom


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
    assert payload.purpose == PURPOSE_AGENT_MAIN
    assert payload.user_id == test_user.id
    assert payload.session_id == "sess-int"
    assert payload.request_id == "req-int"
    assert payload.messages, "messages should be non-empty when LLM is dispatched"
    assert isinstance(payload.started_at, datetime)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_observer_fires_again_after_context_length_trim_retry(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """When the first LLM call raises ``ContextLengthExceededError`` the
    agent trims history and retries. The retry sends a different (smaller)
    payload, so the observer must fire a second time with
    ``purpose=PURPOSE_AGENT_FOLLOWUP``. Without this, premium would never
    capture the post-trim shape that actually went over the wire."""
    mock_amessages.side_effect = [
        ContextLengthExceededError("too big"),
        make_text_response("ok after trim"),
    ]

    received: list[LLMRequestPayload] = []

    async def obs(payload: LLMRequestPayload) -> None:
        received.append(payload)

    set_llm_request_observer(obs)
    try:
        agent = ClawboltAgent(user=test_user)
        await agent.process_message("hello there")
    finally:
        set_llm_request_observer(None)

    purposes = [p.purpose for p in received]
    assert PURPOSE_AGENT_MAIN in purposes
    assert PURPOSE_AGENT_FOLLOWUP in purposes
    # The follow-up payload's messages list reflects the trimmed history,
    # so it should be a different object from (and not necessarily the
    # same length as) the main payload.
    main = next(p for p in received if p.purpose == PURPOSE_AGENT_MAIN)
    followup = next(p for p in received if p.purpose == PURPOSE_AGENT_FOLLOWUP)
    assert main.messages is not followup.messages


# ---------------------------------------------------------------------------
# Integration: observer fires from compaction and heartbeat dispatch sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.compaction.amessages")
async def test_observer_fires_from_compaction(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """``compact_session`` dispatches an LLM call too, and observers that
    care about token-efficiency analysis want it: compaction is precisely
    where context-window weight gets compressed. The observer must fire
    with ``purpose='compaction'``."""
    import json

    from backend.app.agent.compaction import compact_session
    from backend.app.agent.messages import UserMessage as AgentUserMessage

    mock_amessages.return_value = make_text_response(
        json.dumps({"memory_update": "ok", "summary": ""})
    )

    received: list[LLMRequestPayload] = []

    async def obs(payload: LLMRequestPayload) -> None:
        received.append(payload)

    set_llm_request_observer(obs)
    try:
        await compact_session(
            test_user.id,
            [AgentUserMessage(content="hi", seq=1)],
        )
    finally:
        set_llm_request_observer(None)

    assert any(p.purpose == "compaction" for p in received)
    payload = next(p for p in received if p.purpose == "compaction")
    assert payload.user_id == test_user.id
    assert payload.session_id is None
    # Compaction sends a synthetic prompt; the era-marker field has no
    # meaning here and must be None so observers don't conflate it with
    # an agent-loop era.
    assert payload.min_message_seq_in_prompt is None


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


# ---------------------------------------------------------------------------
# Response observer (unit tests, no agent integration)
# ---------------------------------------------------------------------------


def _make_response_payload() -> LLMResponsePayload:
    started = datetime.now(UTC)
    return LLMResponsePayload(
        schema_version=1,
        purpose=PURPOSE_AGENT_MAIN,
        user_id="user-1",
        session_id="sess-1",
        request_id="req-1",
        model="claude-test",
        provider="anthropic",
        content_blocks=[{"type": "text", "text": "hi"}],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=2,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        started_at=started,
        completed_at=started,
    )


def test_response_payload_schema_version_is_one() -> None:
    """Pin the response-payload schema version, same rationale as the
    request payload: bumps must be coordinated with every observer."""
    assert _make_response_payload().schema_version == 1


def test_default_response_observer_is_none() -> None:
    assert get_llm_response_observer() is None


def test_set_and_get_response_observer() -> None:
    async def obs(_: LLMResponsePayload) -> None:
        return None

    set_llm_response_observer(obs)
    assert get_llm_response_observer() is obs

    set_llm_response_observer(None)
    assert get_llm_response_observer() is None


@pytest.mark.asyncio()
async def test_emit_response_calls_registered_observer() -> None:
    received: list[LLMResponsePayload] = []

    async def obs(payload: LLMResponsePayload) -> None:
        received.append(payload)

    set_llm_response_observer(obs)
    payload = _make_response_payload()
    await emit_llm_response(payload)
    assert received == [payload]


@pytest.mark.asyncio()
async def test_emit_response_is_noop_when_no_observer() -> None:
    # Must not raise.
    await emit_llm_response(_make_response_payload())


@pytest.mark.asyncio()
async def test_emit_response_swallows_observer_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A misbehaving response observer cannot break the agent turn."""

    async def boom(_: LLMResponsePayload) -> None:
        raise RuntimeError("observer blew up")

    set_llm_response_observer(boom)
    with caplog.at_level(logging.ERROR, logger="backend.app.agent.observer"):
        await emit_llm_response(_make_response_payload())
    assert any("observer raised" in rec.message for rec in caplog.records)
    # Still registered after failure: a transient observer issue does
    # not silently deregister it.
    assert get_llm_response_observer() is boom


# ---------------------------------------------------------------------------
# Response observer (integration: fires from inside _call_llm_with_retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_response_observer_fires_from_agent_loop(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """The response observer fires after each ``amessages`` call returns,
    with the same ``request_id`` / ``started_at`` that went into the
    matching request payload so observers can pair the two without
    relying on call ordering across concurrent agent loops."""
    mock_amessages.return_value = make_text_response("hello back")

    requests: list[LLMRequestPayload] = []
    responses: list[LLMResponsePayload] = []

    async def req_obs(p: LLMRequestPayload) -> None:
        requests.append(p)

    async def resp_obs(p: LLMResponsePayload) -> None:
        responses.append(p)

    set_llm_request_observer(req_obs)
    set_llm_response_observer(resp_obs)
    try:
        agent = ClawboltAgent(user=test_user, session_id="sess-rsp", request_id="req-rsp")
        await agent.process_message("hi")
    finally:
        set_llm_request_observer(None)
        set_llm_response_observer(None)

    assert requests, "request observer should have fired"
    assert responses, "response observer should have fired"
    req = requests[0]
    resp = responses[0]
    # Pairing fields echo across both payloads.
    assert resp.request_id == req.request_id == "req-rsp"
    assert resp.session_id == req.session_id == "sess-rsp"
    assert resp.user_id == req.user_id == test_user.id
    assert resp.started_at == req.started_at
    # Purpose carries through unchanged.
    assert resp.purpose == req.purpose == PURPOSE_AGENT_MAIN
    # Content blocks are serialized to plain dicts (no pydantic objects).
    for block in resp.content_blocks:
        assert isinstance(block, dict)
        assert "type" in block


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_response_observer_purpose_followup_after_trim_retry(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """When the first round is rejected by the provider for
    ``ContextLengthExceeded`` the retry's request is tagged
    ``PURPOSE_AGENT_FOLLOWUP``. The matching response payload must use the
    same purpose so observers can pair them by request_id without
    fixing up the label."""
    mock_amessages.side_effect = [
        ContextLengthExceededError("too big"),
        make_text_response("ok after trim"),
    ]

    responses: list[LLMResponsePayload] = []

    async def resp_obs(p: LLMResponsePayload) -> None:
        responses.append(p)

    set_llm_response_observer(resp_obs)
    try:
        agent = ClawboltAgent(user=test_user)
        await agent.process_message("hi")
    finally:
        set_llm_response_observer(None)

    assert any(r.purpose == PURPOSE_AGENT_FOLLOWUP for r in responses)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_response_observer_exception_does_not_crash_agent_loop(
    mock_amessages: MagicMock,
    test_user: User,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same safety guarantee as the request observer: a raising response
    observer cannot break the user-facing turn."""
    mock_amessages.return_value = make_text_response("ok")

    async def boom(_: LLMResponsePayload) -> None:
        raise RuntimeError("response observer blew up")

    set_llm_response_observer(boom)
    try:
        with caplog.at_level(logging.ERROR, logger="backend.app.agent.observer"):
            agent = ClawboltAgent(user=test_user)
            await agent.process_message("still ok")
    finally:
        set_llm_response_observer(None)

    assert any("observer raised" in rec.message for rec in caplog.records)


@pytest.mark.asyncio()
@patch("backend.app.agent.compaction.amessages")
async def test_response_observer_fires_from_compaction(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """Compaction also dispatches an LLM call; the response observer must
    fire there too so post-incident forensics have parity with the
    request observer."""
    import json

    from backend.app.agent.compaction import compact_session
    from backend.app.agent.messages import UserMessage as AgentUserMessage

    mock_amessages.return_value = make_text_response(
        json.dumps({"memory_update": "ok", "summary": ""})
    )

    received: list[LLMResponsePayload] = []

    async def obs(payload: LLMResponsePayload) -> None:
        received.append(payload)

    set_llm_response_observer(obs)
    try:
        await compact_session(
            test_user.id,
            [AgentUserMessage(content="hi", seq=1)],
        )
    finally:
        set_llm_response_observer(None)

    assert any(p.purpose == "compaction" for p in received)
    payload = next(p for p in received if p.purpose == "compaction")
    assert payload.user_id == test_user.id
    assert payload.session_id is None
    # Content is serialized as plain dicts.
    assert payload.content_blocks
    for block in payload.content_blocks:
        assert isinstance(block, dict)
        assert "type" in block


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.amessages")
async def test_response_observer_fires_from_heartbeat_decision(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """Heartbeat decision is the third LLM dispatch site (alongside agent
    loop and compaction). Pair the request observer's coverage with
    response-side coverage so we never have a half-instrumented
    purpose."""
    from backend.app.agent.heartbeat import evaluate_heartbeat_need

    # The heartbeat decider parses a plain text response into a no-send
    # decision when no tool_use is present; the parser tolerates an
    # empty / "skip" reply so the dispatch path completes without us
    # standing up a fake tool_use block.
    mock_amessages.return_value = make_text_response("skip")

    received: list[LLMResponsePayload] = []

    async def obs(payload: LLMResponsePayload) -> None:
        received.append(payload)

    set_llm_response_observer(obs)
    try:
        # We only care that the observer fired before any downstream
        # parsing or store interaction; the rest of the heartbeat path
        # is exercised by ``test_heartbeat.py``. Suppress any parse-time
        # failure raised by the synthetic mock response.
        with contextlib.suppress(Exception):
            await evaluate_heartbeat_need(test_user)
    finally:
        set_llm_response_observer(None)

    assert any(p.purpose == "heartbeat_decision" for p in received), (
        "response observer must fire for heartbeat decision"
    )
    payload = next(p for p in received if p.purpose == "heartbeat_decision")
    assert payload.user_id == test_user.id
    assert payload.session_id is None
