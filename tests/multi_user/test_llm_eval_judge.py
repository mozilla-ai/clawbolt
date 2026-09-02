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

from backend.app.services.llm_eval.judge import (
    _describe,
    candidate_in_slot_a,
    judge_turn,
)
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

    ``stop_reason`` carries the same default the providers send on a normal
    completion, so a test that does not care about truncation does not have
    to say so.
    """

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [TextBlock(type="text", text=text)]
        self.stop_reason = stop_reason


def _judge_reply(**payload: Any) -> AsyncMock:
    return AsyncMock(return_value=_Response(json.dumps(payload)))


BASELINE = ModelCallResult(
    provider="anthropic",
    model="incumbent",
    tool_calls=[ToolCall(name="lookup", arguments={"query": "invoice"})],
)
CANDIDATE = ModelCallResult(provider="anthropic", model="candidate", text="I'll take a look.")

TURN_TEXT = "where is invoice 42?"


def _seq_for_slot(candidate_is_a: bool) -> int:
    """Find a seq whose turn puts the candidate in the requested slot.

    Asks the implementation rather than recomputing the hash, so this cannot
    drift into testing a second copy of the assignment rule.
    """
    for seq in range(1, 500):
        sample = ReplaySample(seq=seq, timestamp="", message_context=TURN_TEXT)
        if candidate_in_slot_a(sample) is candidate_is_a:
            return seq
    raise AssertionError("no seq produced the requested slot")


SEQ_CANDIDATE_IS_A = _seq_for_slot(True)
SEQ_CANDIDATE_IS_B = _seq_for_slot(False)


async def _judge(seq: int, mock: AsyncMock) -> tuple[JudgeVerdict, str]:
    with patch("backend.app.services.llm_eval.judge.amessages", mock):
        return await judge_turn(
            ReplaySample(seq=seq, timestamp="", message_context=TURN_TEXT),
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


def test_slot_assignment_varies_across_a_realistic_transcript() -> None:
    """Regression: the assignment must not alias to message-seq parity.

    A transcript alternates inbound and outbound rows, so every replayable
    turn has an odd seq. An assignment keyed on ``seq % 2`` is constant for a
    whole run, which silently pins the candidate to one slot and makes the
    blinding decorative.
    """
    slots = {
        candidate_in_slot_a(ReplaySample(seq=seq, timestamp="", message_context=f"turn {seq}"))
        for seq in range(1, 60, 2)
    }
    assert slots == {True, False}
