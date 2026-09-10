"""What a usage row claims about cost when a gateway served the call.

``llm_usage_logs.cost`` is priced from ``(provider, model)``. Behind an
endpoint the provider names the dialect the request was written in, not
whoever billed the tokens, so a price-list hit on that pair is a coincidence.
These pin that an unpriced endpoint records the usage and declines to invent
the cost, rather than reporting the dialect vendor's rate.
"""

from decimal import Decimal

from backend.app.agent.stores import _build_llm_usage_log
from backend.app.models import LLMUsageLog

# A pair genai-prices knows, so a wrong answer here would be a real number
# rather than a zero that happens to look right.
PRICED_MODEL = "claude-sonnet-4-20250514"


def _row(*, endpoint: str = "", priced: bool = True) -> LLMUsageLog:
    return _build_llm_usage_log(
        user_id="u1",
        model=PRICED_MODEL,
        prompt_tokens=1000,
        completion_tokens=100,
        purpose="agent_main",
        provider="anthropic",
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
        endpoint=endpoint,
        priced=priced,
    )


def test_a_bare_provider_is_priced_as_before() -> None:
    row = _row()
    assert row.endpoint == ""
    assert row.pricing_available is True
    assert row.cost > Decimal("0")


def test_an_unpriced_endpoint_records_no_cost() -> None:
    """Zero with ``pricing_available=False``, not the dialect's rate.

    The tokens still count; only the money is withheld, because the figure
    would describe a vendor that never saw the request.
    """
    row = _row(endpoint="otari", priced=False)
    assert row.endpoint == "otari"
    assert row.pricing_available is False
    assert row.cost == Decimal("0.000000")
    assert row.input_tokens == 1000
    assert row.total_tokens == 1100


def test_a_priced_endpoint_still_gets_a_cost() -> None:
    """``unpriced`` is opt-in per endpoint, not implied by having one."""
    row = _row(endpoint="proxy", priced=True)
    assert row.endpoint == "proxy"
    assert row.pricing_available is True
    assert row.cost > Decimal("0")


def test_an_unknown_model_is_still_flagged_without_an_endpoint() -> None:
    """The pre-existing reason a cost can be missing keeps working."""
    row = _build_llm_usage_log(
        user_id="u1",
        model="not-a-real-model-xyz",
        prompt_tokens=10,
        completion_tokens=1,
        purpose="agent_main",
        provider="anthropic",
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    assert row.pricing_available is False
    assert row.cost == Decimal("0.000000")
