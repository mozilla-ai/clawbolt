"""Blinding and verdict mapping for the model-swap evaluator's judge.

The judge sees two unlabeled responses. Every test here is really the same
test: whichever slot the candidate landed in, the verdict has to come back
pointing at the candidate. Getting that mapping backwards would invert every
quality signal in the report while looking entirely plausible.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from any_llm.types.messages import TextBlock

from backend.app.services.llm_eval.judge import _describe, judge_turn
from backend.app.services.llm_eval.types import (
    JudgeVerdict,
    ModelCallResult,
    ReplaySample,
    ToolCall,
)


class _Response:
    """Minimal stand-in for ``MessageResponse``.

    The content block must be a real ``TextBlock``: ``get_response_text``
    filters by ``isinstance``, so a duck-typed stub silently reads as an
    empty response and every mapping assertion below would pass for the
    wrong reason.
    """

    def __init__(self, text: str) -> None:
        self.content = [TextBlock(type="text", text=text)]


def _judge_reply(**payload: Any) -> AsyncMock:
    return AsyncMock(return_value=_Response(json.dumps(payload)))


BASELINE = ModelCallResult(
    provider="anthropic",
    model="incumbent",
    tool_calls=[ToolCall(name="lookup", arguments={"query": "invoice"})],
)
CANDIDATE = ModelCallResult(provider="anthropic", model="candidate", text="I'll take a look.")

# ``judge_turn`` assigns slots by ``seq % 2``: even seq puts the candidate in
# slot A, odd puts it in B. Pinning both parities is the whole point.
SEQ_CANDIDATE_IS_A = 2
SEQ_CANDIDATE_IS_B = 3


async def _judge(seq: int, mock: AsyncMock) -> tuple[JudgeVerdict, str]:
    with patch("backend.app.services.llm_eval.judge.amessages", mock):
        return await judge_turn(
            ReplaySample(seq=seq, timestamp="", message_context="where is invoice 42?"),
            BASELINE,
            CANDIDATE,
            provider="anthropic",
            model="incumbent",
        )


@pytest.mark.asyncio()
async def test_winner_a_maps_to_candidate_when_candidate_is_a() -> None:
    verdict, rationale = await _judge(
        SEQ_CANDIDATE_IS_A, _judge_reply(winner="A", unsafe="none", rationale="acted")
    )
    assert verdict is JudgeVerdict.CANDIDATE_BETTER
    assert rationale == "acted"


@pytest.mark.asyncio()
async def test_winner_a_maps_to_incumbent_when_candidate_is_b() -> None:
    verdict, _ = await _judge(
        SEQ_CANDIDATE_IS_B, _judge_reply(winner="A", unsafe="none", rationale="acted")
    )
    assert verdict is JudgeVerdict.CANDIDATE_WORSE


@pytest.mark.asyncio()
async def test_winner_b_maps_to_candidate_when_candidate_is_b() -> None:
    verdict, _ = await _judge(
        SEQ_CANDIDATE_IS_B, _judge_reply(winner="B", unsafe="none", rationale="acted")
    )
    assert verdict is JudgeVerdict.CANDIDATE_BETTER


@pytest.mark.asyncio()
async def test_equivalent_passes_through() -> None:
    verdict, _ = await _judge(
        SEQ_CANDIDATE_IS_A, _judge_reply(winner="equivalent", unsafe="none", rationale="same")
    )
    assert verdict is JudgeVerdict.EQUIVALENT


@pytest.mark.asyncio()
async def test_unsafe_flag_on_the_candidate_slot_blocks() -> None:
    verdict, _ = await _judge(
        SEQ_CANDIDATE_IS_A, _judge_reply(winner="B", unsafe="A", rationale="texts the wrong person")
    )
    assert verdict is JudgeVerdict.CANDIDATE_UNSAFE


@pytest.mark.asyncio()
async def test_unsafe_flag_on_the_incumbent_does_not_credit_the_candidate() -> None:
    """An unsafe incumbent is worth recording, but it is not evidence to switch."""
    verdict, rationale = await _judge(
        SEQ_CANDIDATE_IS_B, _judge_reply(winner="B", unsafe="A", rationale="bad call")
    )
    assert verdict is JudgeVerdict.EQUIVALENT
    assert "incumbent flagged unsafe" in rationale


@pytest.mark.asyncio()
async def test_prose_around_the_json_is_tolerated() -> None:
    mock = AsyncMock(
        return_value=_Response(
            'Here is my assessment:\n{"winner": "equivalent", "unsafe": "none", '
            '"rationale": "both fine"}\nHope that helps.'
        )
    )
    verdict, _ = await _judge(SEQ_CANDIDATE_IS_A, mock)
    assert verdict is JudgeVerdict.EQUIVALENT


@pytest.mark.asyncio()
async def test_unparseable_output_is_recorded_not_raised() -> None:
    mock = AsyncMock(return_value=_Response("I cannot decide."))
    verdict, _ = await _judge(SEQ_CANDIDATE_IS_A, mock)
    assert verdict is JudgeVerdict.JUDGE_FAILED


@pytest.mark.asyncio()
async def test_provider_failure_is_recorded_not_raised() -> None:
    mock = AsyncMock(side_effect=RuntimeError("gateway down"))
    verdict, rationale = await _judge(SEQ_CANDIDATE_IS_A, mock)
    assert verdict is JudgeVerdict.JUDGE_FAILED
    assert "gateway down" in rationale


def test_description_never_names_the_model() -> None:
    """The judge must not be able to tell which response is the incumbent."""
    rendered = _describe(BASELINE)
    assert "incumbent" not in rendered
    assert "anthropic" not in rendered
    assert "lookup" in rendered
