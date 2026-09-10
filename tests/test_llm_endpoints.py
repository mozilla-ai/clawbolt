"""Named LLM endpoints: resolution, capability overrides, and reasoning shape.

The regression these guard is issue #1544. A gateway fronting somebody else's
model was reached by setting ``LLM_PROVIDER`` to the dialect it spoke, which
left three further decisions keyed on a name that no longer described the
vendor. The worst of them was fatal: an Anthropic thinking budget translated
into ``reasoning_effort`` and sent alongside function tools, which the far side
refuses outright.
"""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import backend.app.services.llm_endpoints as _module
from backend.app.database import db_session_async
from backend.app.models import LLMEndpoint
from backend.app.services.llm_endpoints import (
    UnknownLLMEndpointError,
    get_endpoint,
    list_endpoints,
    reset_llm_endpoint_cache,
    resolve_target,
    role_selection,
)
from backend.app.services.llm_service import LLMTarget, ReasoningStyle


@pytest.fixture(autouse=True)
async def _clear_endpoint_cache() -> AsyncGenerator[None]:
    """The endpoint cache is process-global, so it must not cross tests."""
    reset_llm_endpoint_cache()
    yield
    reset_llm_endpoint_cache()


async def _add_endpoint(**kwargs: str) -> None:
    """Insert one endpoint and drop the cache, as the CRUD route does."""
    async with db_session_async() as db:
        db.add(LLMEndpoint(**kwargs))
        await db.commit()
    reset_llm_endpoint_cache()


# ---------------------------------------------------------------------------
# Reasoning shape
# ---------------------------------------------------------------------------


class TestReasoningKwargs:
    """The one decision that was fatal rather than merely wrong."""

    def test_thinking_style_sends_a_budget_dict(self) -> None:
        target = LLMTarget(provider="anthropic", model="m")
        assert target.reasoning_kwargs("high") == {
            "thinking": {"type": "enabled", "budget_tokens": 24576}
        }

    def test_effort_style_sends_the_scalar_instead(self) -> None:
        target = LLMTarget(provider="openai", model="m", reasoning_style=ReasoningStyle.EFFORT)
        assert target.reasoning_kwargs("high") == {"reasoning_effort": "high"}

    def test_none_style_sends_no_reasoning_key_at_all(self) -> None:
        """Not ``reasoning_effort="none"``: the key must be absent.

        The endpoint this exists for rejects the parameter's presence when
        tools are also on the request, so any value fails and only omission
        gets the call through.
        """
        target = LLMTarget(provider="openai", model="m", reasoning_style=ReasoningStyle.NONE)
        assert target.reasoning_kwargs("high") == {}
        assert target.reasoning_kwargs("none") == {}

    @pytest.mark.parametrize("effort", ["auto", ""])
    @pytest.mark.parametrize(
        "style", [ReasoningStyle.THINKING, ReasoningStyle.EFFORT, ReasoningStyle.NONE]
    )
    def test_auto_defers_to_the_provider_for_every_style(
        self, style: ReasoningStyle, effort: str
    ) -> None:
        target = LLMTarget(provider="p", model="m", reasoning_style=style)
        assert target.reasoning_kwargs(effort) == {}

    def test_disabled_is_expressible_in_the_thinking_shape(self) -> None:
        target = LLMTarget(provider="anthropic", model="m")
        assert target.reasoning_kwargs("none") == {"thinking": {"type": "disabled"}}


class TestConnectionKwargs:
    def test_api_key_is_omitted_when_the_target_has_none(self) -> None:
        """Omission is load-bearing: it is what lets any-llm read the env var."""
        target = LLMTarget(provider="anthropic", model="m", api_base="https://x")
        assert target.connection_kwargs() == {
            "model": "m",
            "provider": "anthropic",
            "api_base": "https://x",
        }

    def test_api_key_is_passed_when_the_endpoint_carries_one(self) -> None:
        target = LLMTarget(provider="anthropic", model="m", api_key="sk-test")
        assert target.connection_kwargs()["api_key"] == "sk-test"


# ---------------------------------------------------------------------------
# Role pairing
# ---------------------------------------------------------------------------


class TestRoleSelection:
    def test_a_role_naming_nothing_follows_the_primary(self) -> None:
        assert role_selection("", "", "otari", "anthropic") == ("otari", "anthropic")

    def test_a_role_naming_a_bare_provider_drops_the_primary_endpoint(self) -> None:
        """The trap this rule exists for.

        Inheriting field by field would keep ``otari`` here, and an
        endpoint's dialect supersedes the provider beside it, so the operator
        would have set ``openai`` and still been sent to Otari as Anthropic.
        """
        assert role_selection("", "openai", "otari", "anthropic") == ("", "openai")

    def test_a_role_naming_its_own_endpoint_keeps_it(self) -> None:
        assert role_selection("local", "", "otari", "anthropic") == ("local", "")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolveWithoutEndpoint:
    async def test_bare_provider_keeps_the_global_api_base(self) -> None:
        target = await resolve_target(
            endpoint="", provider="anthropic", model="m", api_base="https://gw.test"
        )
        assert target.endpoint == ""
        assert target.provider == "anthropic"
        assert target.api_base == "https://gw.test"
        assert target.api_key is None

    async def test_capabilities_come_from_the_dialect(self) -> None:
        """Unchanged behavior for a deployment that names no endpoint."""
        native = await resolve_target(endpoint="", provider="anthropic", model="m")
        bridge = await resolve_target(endpoint="", provider="fireworks", model="m")
        assert native.honors_cache_control is True
        assert bridge.honors_cache_control is False
        assert native.reasoning_style is ReasoningStyle.THINKING
        assert native.priced is True


class TestResolveWithEndpoint:
    async def test_endpoint_supplies_dialect_base_and_key(self) -> None:
        await _add_endpoint(
            name="otari",
            dialect="anthropic",
            base_url="https://ai.example.test",
            api_key="sk-endpoint",
        )
        # A provider is passed alongside and must lose to the dialect.
        target = await resolve_target(
            endpoint="otari", provider="openai", model="m", api_base="https://ignored.test"
        )
        assert target.provider == "anthropic"
        assert target.api_base == "https://ai.example.test"
        assert target.api_key == "sk-endpoint"
        assert target.endpoint == "otari"

    async def test_capability_columns_override_the_dialect(self) -> None:
        """The whole point: a gateway that does not behave like its dialect."""
        await _add_endpoint(
            name="gw",
            dialect="anthropic",
            base_url="https://gw.test",
            cache_control="never",
            reasoning="none",
            pricing="unpriced",
        )
        target = await resolve_target(endpoint="gw", provider="", model="m")
        assert target.honors_cache_control is False
        assert target.reasoning_style is ReasoningStyle.NONE
        assert target.priced is False

    async def test_cache_control_always_marks_a_bridge_dialect(self) -> None:
        await _add_endpoint(name="fwd", dialect="fireworks", cache_control="always")
        target = await resolve_target(endpoint="fwd", provider="", model="m")
        assert target.honors_cache_control is True

    async def test_prompt_cache_never_outranks_an_endpoint_asking_for_always(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deployment-wide kill switch has to stay trustworthy."""
        from backend.app.config import settings

        await _add_endpoint(name="fwd", dialect="anthropic", cache_control="always")
        monkeypatch.setattr(settings, "llm_prompt_cache", "never")
        target = await resolve_target(endpoint="fwd", provider="", model="m")
        assert target.honors_cache_control is False

    async def test_unknown_reasoning_value_falls_back_to_thinking(self) -> None:
        await _add_endpoint(name="odd", dialect="anthropic", reasoning="sideways")
        target = await resolve_target(endpoint="odd", provider="", model="m")
        assert target.reasoning_style is ReasoningStyle.THINKING

    async def test_missing_endpoint_raises_rather_than_falling_back(self) -> None:
        """A silent fallback would send the dialect vendor's key to the vendor.

        That is the opposite of what the operator configured, and it would
        happen on a typo, so it has to be loud.
        """
        with pytest.raises(UnknownLLMEndpointError, match="typo-endpoint"):
            await resolve_target(
                endpoint="typo-endpoint", provider="anthropic", model="m", api_base=None
            )


class TestEndpointCache:
    async def test_a_new_endpoint_is_invisible_until_the_cache_is_dropped(self) -> None:
        """Documents why every write path calls ``reset_llm_endpoint_cache``."""
        assert await get_endpoint("late") is None
        async with db_session_async() as db:
            db.add(LLMEndpoint(name="late", dialect="anthropic"))
            await db.commit()
        assert await get_endpoint("late") is None
        reset_llm_endpoint_cache()
        row = await get_endpoint("late")
        assert row is not None and row.dialect == "anthropic"

    async def test_listing_is_name_ordered(self) -> None:
        await _add_endpoint(name="zeta", dialect="anthropic")
        await _add_endpoint(name="alpha", dialect="openai")
        assert [e.name for e in await list_endpoints()] == ["alpha", "zeta"]

    async def test_empty_name_never_hits_the_database(self) -> None:
        assert await get_endpoint("") is None

    async def test_a_reset_during_a_load_is_not_lost(self) -> None:
        """A load that starts before a reset must not publish over it.

        The reader awaits its SELECT. A write that commits and invalidates
        during that await would otherwise have its invalidation overwritten
        by the reader's pre-write rows, and the stale cache would survive
        until the next write or a restart: the endpoint saves and is still
        missing from the listing, and raises when selected.
        """
        await _add_endpoint(name="first", dialect="anthropic")

        real_execute = AsyncSession.execute

        async def execute_then_invalidate(self: AsyncSession, *args: Any, **kw: Any) -> Any:
            result = await real_execute(self, *args, **kw)
            # Stand in for a concurrent write landing mid-SELECT.
            reset_llm_endpoint_cache()
            return result

        with patch.object(AsyncSession, "execute", execute_then_invalidate):
            await get_endpoint("first")

        # The cache must not be holding the rows read across that reset.
        assert _module._cache is None
