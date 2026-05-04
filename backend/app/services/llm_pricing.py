"""LLM cost computation backed by the ``genai-prices`` library.

Replaces the hand-rolled per-model pricing table this module used to
keep in code with a thin wrapper around `pydantic/genai-prices
<https://github.com/pydantic/genai-prices>`_, the community-maintained
source of truth for provider pricing. Adding a new model now means
``uv lock --upgrade-package genai-prices``; we no longer carry the
risk of stale rates drifting out of sync with what the providers
actually charge.

The library's price table is bundled at install time, so this module
makes no network call. Pricing data refreshes when we bump the
``genai-prices`` dependency.

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


# Map our canonical model-name prefixes to ``genai-prices`` provider
# ids. Supplying the provider id skips the library's auto-detection
# scan and disambiguates models whose names appear under more than one
# provider. Unmapped prefixes fall through with ``provider_id=None``;
# the library then probes all providers.
_PROVIDER_ID_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("gemini-", "google"),
)


def _provider_id_for(model: str) -> str | None:
    """Best-effort provider id for *model*, or ``None`` for autodetection."""
    for prefix, pid in _PROVIDER_ID_BY_PREFIX:
        if model.startswith(prefix):
            return pid
    return None


def is_known_model(model: str) -> bool:
    """Whether ``genai-prices`` can price this model.

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
            provider_id=_provider_id_for(model),
        )
    except (LookupError, ValueError):
        return False
    return True


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
) -> Decimal:
    """Return the dollar cost for a single LLM call as a 6-decimal Decimal.

    Charges via ``genai-prices``, which understands per-provider
    accounting quirks (Anthropic's input bucket includes cached tokens,
    cache-write surcharges, cache-read discounts, OpenAI's prompt-cache
    discount, etc.) so the caller does not have to.

    Returns ``Decimal('0.000000')`` for models the library does not
    know; the caller should log a warning so a missing model produces
    a ``genai-prices`` upgrade rather than silent zero-cost rows
    forever.
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
            provider_id=_provider_id_for(model),
        )
    except (LookupError, ValueError):
        return Decimal("0.000000")
    return result.total_price.quantize(_QUANT)
