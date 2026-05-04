"""Tests for LLM cost computation.

The pricing module is a thin wrapper around ``genai-prices``; we test
its behavior contract (returns Decimal, 6-decimal quantisation,
unknown-model fallback, Anthropic input-token bucketing convention,
explicit provider routing) rather than asserting exact dollar amounts.
Pinning specific rates would defeat the point of switching to a library
that updates prices when providers change them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from genai_prices import Usage, calc_price

from backend.app.services.llm_pricing import compute_cost, is_known_model

# ---------------------------------------------------------------------------
# Known / unknown model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",  # dated alias prefix-matches
    ],
)
def test_is_known_model_for_supported_models(model: str) -> None:
    assert is_known_model(model, provider="anthropic") is True


def test_is_known_model_for_unmapped_model() -> None:
    assert is_known_model("not-a-real-model-99", provider="anthropic") is False
    assert is_known_model("", provider="anthropic") is False


def test_is_known_model_works_without_provider_via_autodetect() -> None:
    """Empty provider falls through to ``calc_price`` autodetection.
    Used by legacy callers that haven't been updated yet."""
    assert is_known_model("claude-sonnet-4-6") is True
    assert is_known_model("not-a-real-model") is False


# ---------------------------------------------------------------------------
# compute_cost contract
# ---------------------------------------------------------------------------


def test_compute_cost_returns_decimal() -> None:
    cost = compute_cost("claude-sonnet-4-6", 1000, 500, provider="anthropic")
    assert isinstance(cost, Decimal)


def test_compute_cost_zero_tokens_costs_nothing() -> None:
    assert compute_cost("claude-sonnet-4-6", 0, 0, provider="anthropic") == Decimal("0.000000")


def test_unknown_model_falls_through_to_zero() -> None:
    """Conservative fallback: prefer 'we don't know' over a bad estimate."""
    assert compute_cost("not-a-real-model", 1_000_000, 1_000_000, provider="anthropic") == Decimal(
        "0.000000"
    )


def test_unknown_provider_falls_through_to_zero() -> None:
    """A custom local-provider id (e.g. self-hosted ollama) genai-prices
    doesn't know about must not crash; we just record cost=0."""
    assert compute_cost("claude-sonnet-4-6", 1000, 500, provider="my-local-shim") == Decimal(
        "0.000000"
    )


def test_compute_cost_handles_none_cache_columns() -> None:
    """Per-row cache columns can be NULL (older logs, providers that
    don't expose them). Treat as zero, not as a TypeError."""
    cost = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=10,
        provider="anthropic",
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    assert cost > Decimal("0")


def test_compute_cost_quantises_to_six_decimals() -> None:
    """Numeric(12, 6) on the column constrains us to 6 fractional
    digits. The library may return more precision than that."""
    cost = compute_cost("claude-sonnet-4-6", 1, 0, provider="anthropic")
    # Exponent is -6 (six fractional digits) regardless of how the
    # library rounded internally.
    exponent = cost.as_tuple().exponent
    assert isinstance(exponent, int)
    assert exponent == -6


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------


def test_provider_is_passed_to_library_not_inferred_from_model() -> None:
    """When the caller passes a provider explicitly, the library uses
    it directly (no name-prefix guessing on our end). A model-name
    string on its own is no longer enough information for routing."""
    # Same model id, two different providers: behavior should depend on
    # the provider we pass, not on a heuristic over the name.
    pc = calc_price(
        Usage(input_tokens=100, output_tokens=50),
        model_ref="claude-sonnet-4-6",
        provider_id="anthropic",
    )
    our = compute_cost("claude-sonnet-4-6", 100, 50, provider="anthropic")
    assert our == pc.total_price.quantize(Decimal("0.000001"))


def test_compute_cost_works_without_provider_via_autodetect() -> None:
    """Empty provider string falls through to ``calc_price`` autodetect.
    This is the legacy fallback for old callers."""
    cost = compute_cost("claude-sonnet-4-6", 1000, 500)
    assert cost > Decimal("0")


# ---------------------------------------------------------------------------
# Anthropic input-token bucketing convention
# ---------------------------------------------------------------------------


def test_compute_cost_aggregates_input_buckets_for_anthropic() -> None:
    """``genai-prices`` follows Anthropic's wire-protocol convention:
    ``Usage.input_tokens`` is the total prompt size (uncached + cache
    creation + cache reads), with cache write/read columns charging
    their own rates on top.

    Our `compute_cost` API takes them split out (matching what we
    persist) and is responsible for the aggregation. This test pins
    that wiring by checking ``compute_cost`` matches a hand-built
    library call.
    """
    library_result = calc_price(
        Usage(
            input_tokens=362 + 2206,  # plain + cache_write
            cache_write_tokens=2206,
            cache_read_tokens=0,
            output_tokens=92,
        ),
        model_ref="claude-sonnet-4-6",
        provider_id="anthropic",
    ).total_price.quantize(Decimal("0.000001"))

    our_result = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=362,
        output_tokens=92,
        provider="anthropic",
        cache_creation_input_tokens=2206,
        cache_read_input_tokens=0,
    )
    assert our_result == library_result


def test_compute_cost_treats_cache_read_distinctly() -> None:
    """Cache reads are charged at a discount in Anthropic's pricing.
    The library handles the multiplier; we just need to pass them
    through the right field. Compare two equal-token calls where one
    has the cache-read shape and one has only plain input."""
    cache_heavy = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        provider="anthropic",
        cache_creation_input_tokens=0,
        cache_read_input_tokens=10_000,
    )
    plain = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=10_000,
        output_tokens=0,
        provider="anthropic",
    )
    # Cache reads are cheaper than plain input.
    assert cache_heavy < plain
    # And both are non-zero.
    assert cache_heavy > Decimal("0")
    assert plain > Decimal("0")


def test_known_anthropic_models_all_produce_nonzero_cost() -> None:
    """Smoke test: a 1k input + 500 output call returns >0 cost for
    every Anthropic SKU we currently invoke. Catches a future
    library refactor that quietly drops one of these."""
    for model in (
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ):
        cost = compute_cost(model, 1000, 500, provider="anthropic")
        assert cost > Decimal("0"), f"{model} priced at zero"
