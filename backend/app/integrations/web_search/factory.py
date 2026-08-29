"""General web search specialist tool.

One tool, one provider seam. The provider is chosen by
``WEB_SEARCH_PROVIDER`` from the ``_PROVIDERS`` registry below, so adding a
backend means writing a module that satisfies ``SearchProvider`` and adding one
entry here. Nothing above this line is provider-specific: the cache, the retry
policy, the error mapping, and the rendering all operate on plain records whose
field names the provider owns.

Deliberately general. Retailer-specific search (store-level pricing, in-store
stock, SKU normalization) is out of scope: it needs per-retailer clients and
gives back a narrower answer than a web query already provides.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.web_search.brave import BraveSearchProvider
from backend.app.integrations.web_search.cache import SearchCache
from backend.app.integrations.web_search.errors import SearchUnavailableError
from backend.app.integrations.web_search.protocol import SearchProvider
from backend.app.integrations.web_search.render import render_records

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Provider registry. Each entry builds a configured ``SearchProvider`` from an
# API key and a timeout. Add a backend here to make it selectable via
# WEB_SEARCH_PROVIDER.
_PROVIDERS: dict[str, Callable[[str, float], SearchProvider]] = {
    "brave": lambda key, timeout: BraveSearchProvider(key, timeout_seconds=timeout),
}

# Shared across users: providers are stateless, so only the cache is a
# singleton. A per-context cache would be built fresh on every message and
# would never register a hit, which is the whole point of having one.
_cache = SearchCache(ttl_seconds=settings.web_search_cache_ttl_seconds)

_RETRY_HINT = (
    "This is an infrastructure failure, not a bad query, so rewording will not "
    "help. One retry is worth it. If it fails again, tell the user you could "
    "not search just now and answer from what you already know, saying so."
)

# The caveat rides on every result rather than living only in the prompt. A
# cached snippet reaches the model as ordinary text with no marker that it is
# months old, and a number lifted from one lands in a customer bid. Putting it
# next to the data keeps it in view when the model decides how to phrase a
# figure. The second sentence is the green-lid case: a listing page puts several
# products' prices side by side, and only the field pairing says which is which.
_RESULT_FOOTER = (
    "These are search results and may be out of date. Any figure you repeat "
    "from them needs its source URL and should be given as a ballpark to "
    "confirm, never as a firm quote. A price belongs to the item named in the "
    "same result: check it matches what was asked for, and give the range when "
    "results disagree. If nothing here carries the detail you needed, the query "
    "was probably too broad and landed on category pages; name the specific "
    "product and size and search once more before telling the user you could "
    "not find it. Do not infer a figure from a page that does not state one."
)


# Bounds on what the agent may ask for. The ceiling is the lowest limit across
# supported providers (Brave rejects count above 20).
_MIN_RESULTS = 1
_MAX_RESULTS = 20


class WebSearchParams(BaseModel):
    query: str = Field(
        max_length=400,
        description="A natural-language web search query.",
    )
    max_results: int | None = Field(
        default=None,
        description=(
            "How many results to return, 1 to 20. Omit for the default. Ask "
            "for fewer when checking a single fact, more when comparing "
            "prices or options across suppliers."
        ),
    )
    freshness: Literal["pd", "pw", "pm", "py"] | None = Field(
        default=None,
        description=(
            "Restrict results by age: pd past day, pw past week, pm past "
            "month, py past year. Use pm for prices and anything that moves. "
            "Omit it for building codes, specs, and standards, where the "
            "correct answer is often years old and filtering hides it."
        ),
    )


def _format_results(records: list[dict], query: str) -> str:
    """Render provider records as plain text, followed by the sourcing rule."""
    if not records:
        return (
            f'No web results for "{query}". Try different wording, or tell the '
            "user you could not find it."
        )

    return (
        f'{len(records)} web result(s) for "{query}":\n\n'
        + render_records(records)
        + f"\n\n{_RESULT_FOOTER}"
    )


def _create_web_search_tools(provider: SearchProvider, cache: SearchCache) -> list[Tool]:
    """Build the web search tool list.

    ``provider`` and ``cache`` are injected rather than resolved here so tests
    can drive the tool with a stub backend and a fresh cache.
    """

    async def web_search(
        query: str,
        max_results: int | None = None,
        freshness: str | None = None,
    ) -> ToolResult:
        cleaned = query.strip()
        if not cleaned:
            return ToolResult(
                content="Error: empty search query",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Provide a search query describing what to look up.",
            )

        # Clamped rather than rejected: a model asking for 50 wants "lots", and
        # spending a turn on a validation error teaches it nothing useful.
        requested = settings.web_search_max_results if max_results is None else max_results
        resolved = max(_MIN_RESULTS, min(requested, _MAX_RESULTS))

        cache_key = SearchCache.make_key(provider.name, cleaned, resolved, freshness)
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_results(cached, cleaned))

        try:
            records = await provider.search(cleaned, max_results=resolved, freshness=freshness)
        except SearchUnavailableError as exc:
            logger.warning("Web search backend unavailable: query=%r reason=%s", cleaned, exc)
            return ToolResult(
                content="Couldn't reach the web search service.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_RETRY_HINT,
            )
        except httpx.TimeoutException:
            logger.warning("Web search timed out: query=%r", cleaned)
            return ToolResult(
                content="The web search timed out.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_RETRY_HINT,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                # An operator misconfiguration. Retrying and rewording both
                # fail, so say so rather than sending the model in a loop.
                logger.error("Web search auth failed (%d)", status)
                return ToolResult(
                    content="Web search is not configured correctly. Contact admin.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                    hint=(
                        "The API key is missing or rejected. Do not retry. Tell "
                        "the user web search is unavailable and answer from what "
                        "you already know, saying so."
                    ),
                )
            logger.error("Web search error %d for query=%r", status, cleaned)
            return ToolResult(
                content="Couldn't reach the web search service. Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_RETRY_HINT,
            )
        except Exception:
            # Backstop: a provider bug must degrade to a tool error the model
            # can relay, never an exception that ends the message loop.
            logger.exception("Unexpected error in web search: query=%r", cleaned)
            return ToolResult(
                content="Got an unexpected error running that search.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_RETRY_HINT,
            )

        await cache.set(cache_key, records)
        return ToolResult(content=_format_results(records, cleaned))

    return [
        Tool(
            name=ToolName.WEB_SEARCH,
            description=(
                "Search the web. Returns the top results with their source "
                "URLs and whatever details the search engine has for each one. "
                "Write your own search query from what the user asked."
            ),
            function=web_search,
            params_model=WebSearchParams,
            usage_hint=(
                "Results can be out of date, so a figure you repeat from one "
                "needs its source URL and should be framed as a ballpark to "
                "confirm, never a firm quote. This applies to search results "
                "only: totals from connected integrations and rates from the "
                "user's own files are exact and should be stated plainly."
            ),
            # Read-only and stateless: nothing to serialize against.
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: f'Search the web for "{args.get("query", "")}"',
            ),
        ),
    ]


def _resolve_provider() -> SearchProvider | None:
    """Build the configured provider, or None when web search is unavailable."""
    if not settings.web_search_api_key:
        return None
    builder = _PROVIDERS.get(settings.web_search_provider.strip().lower())
    if builder is None:
        logger.error(
            "Unknown WEB_SEARCH_PROVIDER %r; known providers: %s",
            settings.web_search_provider,
            ", ".join(sorted(_PROVIDERS)),
        )
        return None
    return builder(settings.web_search_api_key, settings.web_search_timeout_seconds)


def _web_search_factory(ctx: ToolContext) -> list[Tool]:
    """Factory called by the tool registry."""
    provider = _resolve_provider()
    if provider is None:
        logger.info("web_search factory: not configured, skipping")
        return []

    logger.info("web_search factory: creating tools (provider=%s)", provider.name)
    return _create_web_search_tools(provider, _cache)


async def _web_search_auth_check(ctx: ToolContext) -> str | None:
    """Report readiness to the registry. Returns None when usable.

    Web search is configured by the operator, not connected by the user, so an
    unconfigured install returns a reason that says so. That keeps the tool off
    the schema entirely while still telling the model why, so it says "I can't
    search" instead of inventing an answer or offering an OAuth link that does
    not exist.
    """
    if _resolve_provider() is None:
        return (
            "Web search is not configured on this server. The operator sets "
            "WEB_SEARCH_API_KEY. There is nothing the user can connect."
        )
    return None


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    logger.info("Registering web_search tool factory")
    default_registry.register(
        "web_search",
        _web_search_factory,
        core=False,
        summary="Search the web",
        display_name="Web search",
        dashboard_description="Search the web for current information",
        dashboard_group="Integrations",
        dashboard_group_order=3,
        sub_tools=[
            SubToolInfo(
                ToolName.WEB_SEARCH,
                "Search the web and return results with source links",
                default_permission="always",
            ),
        ],
        auth_check=_web_search_auth_check,
    )


_register()
