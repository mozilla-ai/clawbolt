"""Per-model pricing for LLM usage cost computation.

Used by ``LLMUsageStore.log`` to populate the ``cost`` column on every
``llm_usage_logs`` row instead of leaving it hardcoded at 0. The table
covers the models clawbolt actually invokes via any-llm. Adding a new
provider is one line; unknown models fall back to ``UNKNOWN_PRICING``
which charges nothing rather than guess.

Prices are dollars per million tokens. Cache write surcharge and cache
read discount follow Anthropic's published multipliers (1.25x and 0.1x
of the input rate, respectively); they apply to the cache-specific
token columns we already record on each row. Update the table here when
Anthropic / OpenAI ship new SKUs or revise rates; the database column
type (Numeric(12,6)) accommodates further precision if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPricing:
    """Dollars per million tokens for a single model SKU.

    ``cache_write`` and ``cache_read`` are the rates that apply to the
    ``cache_creation_input_tokens`` and ``cache_read_input_tokens``
    columns respectively. Anthropic charges a 25% premium on cache
    writes and a 90% discount on cache reads relative to ``input``. We
    store these explicitly rather than re-deriving them so providers
    that price caching differently in future are easy to fit.
    """

    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


# Anthropic published rates as of 2026-05.
_SONNET_4_6 = ModelPricing(
    input=Decimal("3.00"),
    output=Decimal("15.00"),
    cache_write=Decimal("3.75"),
    cache_read=Decimal("0.30"),
)
_OPUS_4_7 = ModelPricing(
    input=Decimal("15.00"),
    output=Decimal("75.00"),
    cache_write=Decimal("18.75"),
    cache_read=Decimal("1.50"),
)
_HAIKU_4_5 = ModelPricing(
    input=Decimal("0.80"),
    output=Decimal("4.00"),
    cache_write=Decimal("1.00"),
    cache_read=Decimal("0.08"),
)

UNKNOWN_PRICING = ModelPricing(
    input=Decimal("0"),
    output=Decimal("0"),
    cache_write=Decimal("0"),
    cache_read=Decimal("0"),
)

# Lookup table. Keys match the model identifiers our config emits. Both
# the bare ID and the dated ID forms are covered so a clawbolt instance
# pinned to e.g. ``claude-haiku-4-5-20251001`` resolves correctly.
_PRICING_BY_MODEL: dict[str, ModelPricing] = {
    "claude-sonnet-4-6": _SONNET_4_6,
    "claude-opus-4-7": _OPUS_4_7,
    "claude-haiku-4-5": _HAIKU_4_5,
    "claude-haiku-4-5-20251001": _HAIKU_4_5,
}


def lookup_pricing(model: str) -> ModelPricing:
    """Return pricing for *model*, or ``UNKNOWN_PRICING`` when unmapped.

    A miss is logged at the call site (in ``LLMUsageStore.log``) so we
    notice when a new provider lands without a pricing entry.
    """
    return _PRICING_BY_MODEL.get(model, UNKNOWN_PRICING)


def is_known_model(model: str) -> bool:
    """Whether *model* has a pricing entry. Used by the logger to decide
    when to emit a "no pricing" warning."""
    return model in _PRICING_BY_MODEL


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
) -> Decimal:
    """Return the dollar cost for a single LLM call as a 6-decimal Decimal.

    Charges ``input_tokens`` at the model's input rate, ``output_tokens``
    at the output rate, and the cache columns at their dedicated rates.
    The caller passes ``input_tokens`` as Anthropic reports it: the
    *non-cached* prompt tokens, with cache reads / writes accounted
    separately. We do not double-count.

    Returns ``Decimal('0.000000')`` for models without a pricing entry,
    and quantises the result to six decimal places (matching the
    ``Numeric(12, 6)`` column on ``llm_usage_logs``).
    """
    pricing = lookup_pricing(model)
    cost = (
        pricing.input * Decimal(input_tokens)
        + pricing.output * Decimal(output_tokens)
        + pricing.cache_write * Decimal(cache_creation_input_tokens or 0)
        + pricing.cache_read * Decimal(cache_read_input_tokens or 0)
    ) / Decimal("1000000")
    return cost.quantize(Decimal("0.000001"))
