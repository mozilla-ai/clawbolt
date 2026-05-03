"""Tests for LLM cost computation.

Covers the pricing table and the compute_cost helper. End-to-end wiring
into ``LLMUsageStore.log`` is exercised in ``test_llm_usage_store``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.app.services.llm_pricing import (
    UNKNOWN_PRICING,
    compute_cost,
    is_known_model,
    lookup_pricing,
)

# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ],
)
def test_lookup_returns_real_pricing_for_supported_models(model: str) -> None:
    p = lookup_pricing(model)
    assert p.input > 0
    assert p.output > 0
    assert p.cache_write > 0
    assert p.cache_read > 0


def test_lookup_returns_unknown_pricing_for_unmapped_model() -> None:
    assert lookup_pricing("gpt-99") is UNKNOWN_PRICING
    assert UNKNOWN_PRICING.input == Decimal("0")
    assert UNKNOWN_PRICING.output == Decimal("0")


def test_is_known_model_only_matches_pricing_table() -> None:
    assert is_known_model("claude-sonnet-4-6") is True
    assert is_known_model("") is False
    assert is_known_model("gpt-4") is False


def test_anthropic_cache_multipliers_match_published_rates() -> None:
    """Cache writes are 1.25x input, cache reads are 0.1x input. If
    Anthropic ever revises the multipliers we want a test failure here
    so we update the table deliberately rather than drift silently.
    """
    sonnet = lookup_pricing("claude-sonnet-4-6")
    assert sonnet.cache_write == sonnet.input * Decimal("1.25")
    assert sonnet.cache_read == sonnet.input * Decimal("0.10")

    opus = lookup_pricing("claude-opus-4-7")
    assert opus.cache_write == opus.input * Decimal("1.25")
    assert opus.cache_read == opus.input * Decimal("0.10")


# ---------------------------------------------------------------------------
# compute_cost
# ---------------------------------------------------------------------------


def test_zero_tokens_costs_nothing() -> None:
    assert compute_cost("claude-sonnet-4-6", 0, 0) == Decimal("0.000000")


def test_unknown_model_costs_nothing_regardless_of_tokens() -> None:
    """Conservative fallback: prefer "we don't know" over a bad estimate."""
    assert compute_cost("not-a-real-model", 1_000_000, 1_000_000) == Decimal("0.000000")


def test_sonnet_cost_at_published_rates() -> None:
    """Sonnet 4.6: $3 input, $15 output per 1M tokens. A 1M input + 1M
    output call should cost $18.000000.
    """
    cost = compute_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == Decimal("18.000000")


def test_opus_cost_at_published_rates() -> None:
    """Opus 4.7: $15 input, $75 output per 1M tokens."""
    cost = compute_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    assert cost == Decimal("90.000000")


def test_cache_tokens_charged_at_dedicated_rates() -> None:
    """1M cache write tokens at $3.75/M plus 1M cache read tokens at
    $0.30/M = $4.050000 for sonnet.
    """
    cost = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert cost == Decimal("4.050000")


def test_realistic_jesse_session_cost() -> None:
    """Sanity check using actual numbers from a contractor session
    (heartbeat_decision: ~362 input, ~92 output, ~2206 cache write).
    The cost should be small but non-zero, and the result should
    quantise to 6 decimal places for the Numeric(12, 6) column.
    """
    cost = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=362,
        output_tokens=92,
        cache_creation_input_tokens=2206,
        cache_read_input_tokens=0,
    )
    # 362*$3 + 92*$15 + 2206*$3.75, all per 1M
    expected = (
        Decimal("362") * Decimal("3.00")
        + Decimal("92") * Decimal("15.00")
        + Decimal("2206") * Decimal("3.75")
    ) / Decimal("1000000")
    assert cost == expected.quantize(Decimal("0.000001"))
    # Sanity: roughly 1.5 cents.
    assert cost < Decimal("0.02")
    assert cost > Decimal("0")


def test_compute_cost_handles_none_cache_columns() -> None:
    """Per-row cache columns can be NULL (older logs, providers that
    don't expose them). Treat as zero, not as a TypeError."""
    cost = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=10,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    assert cost > Decimal("0")


def test_compute_cost_quantises_to_six_decimals() -> None:
    """Numeric(12, 6) on the column constrains us to 6 fractional
    digits. A pricing math result with more digits should be quantised
    here so the DB never rejects an insert."""
    # 1 input token: 3/1M = 0.000003
    cost = compute_cost("claude-sonnet-4-6", 1, 0)
    assert cost == Decimal("0.000003")
    # exponent should match the Numeric column's scale (-6 for 6 fractional digits)
    exponent = cost.as_tuple().exponent
    assert isinstance(exponent, int)
    assert exponent == -6
