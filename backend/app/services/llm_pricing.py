"""LLM cost computation backed by the ``genai-prices`` library.

Thin wrapper around `pydantic/genai-prices
<https://github.com/pydantic/genai-prices>`_, the community-maintained
source of truth for provider pricing. Adding a new model means
``uv lock --upgrade-package genai-prices``; we do not keep our own
rate table in code.

The library's price data is bundled at install time, so this module
makes no network call. Pricing data refreshes when we bump the
``genai-prices`` dependency.

The caller passes the any-llm provider id alongside the model name,
since both are known at every dispatch site (see
``settings.llm_provider``). Authoritative provider routing means we
never have to guess "which vendor does this model name belong to" from
prefixes; we just forward what the agent loop already knew.

Used by ``LLMUsageStore.log`` to populate the ``cost`` column on every
``llm_usage_logs`` row instead of leaving it hardcoded at 0.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from genai_prices import Usage, calc_price

logger = logging.getLogger(__name__)


# Cost column on ``llm_usage_logs`` is ``Numeric(12, 6)``; quantise to
# match so the DB never rejects an insert with too many fractional
# digits.
_QUANT = Decimal("0.000001")


def is_known_model(model: str, *, provider: str = "") -> bool:
    """Whether ``genai-prices`` can price this (provider, model) pair.

    Used by the logger to decide when to emit a "no pricing entry"
    warning. The check goes through the same lookup path as
    ``calc_price`` so a true result implies ``compute_cost`` will not
    fall back to zero.
    """
    if not model:
        return False
    try:
        calc_price(
            Usage(input_tokens=1, output_tokens=0),
            model_ref=model,
            provider_id=provider or None,
        )
    except (LookupError, ValueError):
        return False
    return True


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    provider: str = "",
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
) -> Decimal:
    """Return the dollar cost for a single LLM call as a 6-decimal Decimal.

    *provider* is the any-llm provider id (``"anthropic"``, ``"openai"``,
    etc.) under which *model* was invoked. Pass it explicitly: skipping
    autodetection is faster and disambiguates models whose name appears
    under more than one provider.

    Charges via ``genai-prices``, which understands per-provider
    accounting quirks (Anthropic's input bucket includes cached tokens,
    cache-write surcharges, cache-read discounts, OpenAI's prompt-cache
    discount, etc.) so the caller does not have to.

    Returns ``Decimal('0.000000')`` for (provider, model) pairs the
    library does not know; the caller should log a warning so a missing
    model produces a ``genai-prices`` upgrade rather than silent
    zero-cost rows forever.
    """
    cache_creation = cache_creation_input_tokens or 0
    cache_read = cache_read_input_tokens or 0
    # Anthropic (and several other providers) price the full input
    # bucket including cached tokens; the library expects
    # ``input_tokens`` to be the total. Per-bucket multipliers come
    # from the cache-specific fields.
    total_input = input_tokens + cache_creation + cache_read
    try:
        result = calc_price(
            Usage(
                input_tokens=total_input,
                cache_write_tokens=cache_creation,
                cache_read_tokens=cache_read,
                output_tokens=output_tokens,
            ),
            model_ref=model,
            provider_id=provider or None,
        )
    except (LookupError, ValueError):
        return Decimal("0.000000")
    return result.total_price.quantize(_QUANT)
