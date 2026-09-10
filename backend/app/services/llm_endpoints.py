"""Named LLM endpoints, and the one place that turns a selection into a call.

A model selection used to be a ``(provider, model)`` pair plus one global
``LLM_API_BASE``. That works while the provider name and the vendor serving
the request are the same thing. Point the base URL at a gateway and they stop
being the same thing, but four separate decisions are still being read off
that one name:

1. which any-llm adapter to use, i.e. the wire format,
2. whether stamping ``cache_control`` markers is worth doing,
3. how to ask for reasoning,
4. which genai-prices entry describes what the tokens cost.

Only the first is still true for a gateway. The rest become confident and
wrong, and nothing surfaces the error: markers vanish at a hop that drops
them, an Anthropic thinking budget reaches a model that wanted
``reasoning_effort`` (or gets refused outright, which is issue #1544), and a
cost total is reported to five decimal places against a price list for a
vendor that never saw the request.

An :class:`~backend.app.models.LLMEndpoint` splits them apart: ``dialect``
answers (1), and the capability columns answer (2) through (4) explicitly.
:func:`resolve_target` collapses a selection plus its endpoint into an
:class:`~backend.app.services.llm_service.LLMTarget`, and every ``amessages``
call site takes one. Call sites must not re-derive any of these from a
provider name.

A deployment that names no endpoint keeps the old behavior exactly: the
dialect defaults answer all four questions, and ``settings.llm_api_base``
still applies.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.config_store import MASK
from backend.app.database import AsyncSessionLocal
from backend.app.models import LLMEndpoint, Subscription
from backend.app.services.llm_service import (
    LLMTarget,
    ReasoningStyle,
    provider_honors_cache_control,
)

logger = logging.getLogger(__name__)


class UnknownLLMEndpointError(RuntimeError):
    """A selection named an endpoint that does not exist.

    Raised rather than falling back to the bare provider. The fallback would
    send the request to the dialect vendor's own host, carrying the key that
    vendor issued, which is the failure this whole abstraction exists to
    prevent. A typo in an endpoint name has to be loud.
    """


# Endpoint capability columns that mean "follow the dialect".
_AUTO = "auto"


# ---------------------------------------------------------------------------
# Endpoint cache
# ---------------------------------------------------------------------------

# Endpoints are read on every LLM call and written only by an operator, so
# they are cached in process and dropped on write. ``None`` means "not
# loaded"; an empty dict is a valid loaded state.
_cache: dict[str, LLMEndpoint] | None = None
_cache_lock = asyncio.Lock()


def reset_llm_endpoint_cache() -> None:
    """Drop the cache. Called after a write, and between tests."""
    global _cache
    _cache = None


async def _load_endpoints() -> dict[str, LLMEndpoint]:
    """Return every endpoint by name, populating the cache on first use."""
    global _cache
    if _cache is not None:
        return _cache
    async with _cache_lock:
        # Another coroutine may have populated it while we waited.
        if _cache is not None:
            return _cache
        db = AsyncSessionLocal()
        try:
            rows = (await db.execute(select(LLMEndpoint))).scalars().all()
        finally:
            await db.close()
        _cache = {row.name: row for row in rows}
        return _cache


async def get_endpoint(name: str) -> LLMEndpoint | None:
    """Look up one endpoint by name, or None."""
    if not name:
        return None
    return (await _load_endpoints()).get(name)


async def list_endpoints() -> list[LLMEndpoint]:
    """Every endpoint, name-ordered."""
    return sorted((await _load_endpoints()).values(), key=lambda e: e.name)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def upsert_endpoint(
    db: AsyncSession,
    *,
    name: str,
    dialect: str,
    base_url: str,
    api_key: str | None,
    cache_control: str,
    reasoning: str,
    pricing: str,
    notes: str,
) -> LLMEndpoint:
    """Create or replace one endpoint, then drop the cache.

    *api_key* of ``None`` (or the store's mask) leaves the stored key alone,
    so a form the UI never populated can be re-submitted without blanking a
    working credential. An empty string clears it, which is how an operator
    drops one.
    """
    row = (
        await db.execute(select(LLMEndpoint).where(LLMEndpoint.name == name))
    ).scalar_one_or_none()
    if row is None:
        row = LLMEndpoint(name=name)
        db.add(row)
    row.dialect = dialect
    row.base_url = base_url
    if api_key is not None and api_key != MASK:
        row.api_key = api_key
    row.cache_control = cache_control
    row.reasoning = reasoning
    row.pricing = pricing
    row.notes = notes
    await db.commit()
    await db.refresh(row)
    reset_llm_endpoint_cache()
    return row


def endpoint_selectors(name: str) -> list[str]:
    """Setting names that currently select *name*.

    Deleting an endpoint out from under a live selection would turn every
    call through it from working into raising, and the traceback would name
    the endpoint rather than the deletion.
    """
    selectors = {
        "llm_endpoint": settings.llm_endpoint,
        "vision_endpoint": settings.vision_endpoint,
        "heartbeat_endpoint": settings.heartbeat_endpoint,
        "compaction_endpoint": settings.compaction_endpoint,
    }
    return sorted(key for key, value in selectors.items() if value == name)


async def count_endpoint_pins(db: AsyncSession, name: str) -> int:
    """How many per-user overrides pin *name*."""
    return (
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.llm_endpoint_override == name)
        )
    ) or 0


async def delete_endpoint(db: AsyncSession, name: str) -> bool:
    """Delete *name*, returning False when it does not exist."""
    row = (
        await db.execute(select(LLMEndpoint).where(LLMEndpoint.name == name))
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    reset_llm_endpoint_cache()
    return True


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _cache_control_for(row: LLMEndpoint) -> bool:
    """Whether to stamp cache markers for *row*.

    ``llm_prompt_cache="never"`` outranks an endpoint asking for "always":
    it is the deployment-wide kill switch for markers, and an endpoint that
    could override it would make the switch untrustworthy.
    """
    if settings.llm_prompt_cache == "never":
        return False
    if row.cache_control == "always":
        return True
    if row.cache_control == "never":
        return False
    return provider_honors_cache_control(row.dialect)


def _reasoning_style_for(row: LLMEndpoint) -> ReasoningStyle:
    """The reasoning shape for *row*, defaulting to the dialect's."""
    if row.reasoning and row.reasoning != _AUTO:
        try:
            return ReasoningStyle(row.reasoning)
        except ValueError:
            logger.warning(
                "Endpoint %r has unknown reasoning style %r; using %s",
                row.name,
                row.reasoning,
                ReasoningStyle.THINKING,
            )
    return ReasoningStyle.THINKING


async def resolve_target(
    *, endpoint: str, provider: str, model: str, api_base: str | None = None
) -> LLMTarget:
    """Turn a model selection into the target to send it to.

    When *endpoint* is set, its dialect supersedes *provider*: the endpoint
    already states the wire format, and a provider chosen next to it would
    have to agree with that dialect to mean anything, so there is nothing for
    a second opinion to add. The endpoint's own base URL wins over *api_base*
    for the same reason.

    *api_base* is the deployment-wide default for the no-endpoint case, and
    is passed in rather than read here so the caller's own ``settings``
    reference remains the one that decides.
    """
    if endpoint:
        row = await get_endpoint(endpoint)
        if row is None:
            raise UnknownLLMEndpointError(
                f"LLM endpoint {endpoint!r} is not configured. "
                "Create it under Settings > Model, or clear the selection."
            )
        return LLMTarget(
            provider=row.dialect,
            model=model,
            api_base=row.base_url or None,
            api_key=row.api_key or None,
            endpoint=row.name,
            honors_cache_control=_cache_control_for(row),
            reasoning_style=_reasoning_style_for(row),
            priced=row.pricing != "unpriced",
        )
    return LLMTarget(
        provider=provider,
        model=model,
        api_base=api_base,
        endpoint="",
        honors_cache_control=provider_honors_cache_control(provider),
        reasoning_style=ReasoningStyle.THINKING,
        priced=True,
    )


def role_selection(
    role_endpoint: str,
    role_provider: str,
    global_endpoint: str,
    global_provider: str,
) -> tuple[str, str]:
    """Pick the (endpoint, provider) pair for a secondary role.

    The pair is inherited together, not field by field. A role that names
    either half is choosing its own destination and takes neither half from
    the primary selection; a role that names neither follows the primary.
    Inheriting field by field would let a role that names only a provider
    keep the primary's endpoint, whose dialect would then supersede the very
    provider the operator set.

    Pure, and takes its globals as arguments, so a caller's own ``settings``
    reference stays the one that decides. A shared helper reaching for the
    singleton itself would move the decision out of the module that
    configures it.
    """
    if role_endpoint or role_provider:
        return role_endpoint, role_provider
    return global_endpoint, global_provider
