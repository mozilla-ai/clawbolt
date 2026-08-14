"""Supplier pricing specialist tools.

Home Depot product search via SerpApi, a licensed search API that fronts the
retailer on our behalf.

Lowe's search and store lookup are deliberately absent: SerpApi has no engine for
either, and the browser backend that used to serve them was removed. Do not
reintroduce a scraping backend here without legal sign-off.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.supplier_pricing.cache import SupplierCache
from backend.app.integrations.supplier_pricing.errors import SupplierUnavailableError
from backend.app.integrations.supplier_pricing.homedepot import HomeDepotSupplier
from backend.app.integrations.supplier_pricing.protocol import Location, ProductResult

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Shared across all users; the SerpApi client is stateless so only the caches
# need to be singletons.
_cache = SupplierCache()

# Consecutive failures per (supplier, zip), so a retailer that just went down
# stops being asked. The positive cache cannot do this job: it is keyed on the
# exact query string, so a model that rewords its search misses it every time
# and each miss pays a full round trip through every backend.
_OUTAGE_TTL_SECONDS = 90
_outages = SupplierCache(maxsize=500, ttl_seconds=_OUTAGE_TTL_SECONDS)

# Let one retry through before suppressing. A single failed call is as likely to
# be a blip as an outage, and _BACKEND_RETRY_HINT tells the model to try once
# more. Suppression starts at the point where retrying has stopped being
# evidence-gathering and started being a storm.
_OUTAGE_STREAK_BEFORE_SUPPRESSING = 2

# Both failure modes are infrastructure, never the search term. Saying so is the
# whole point of the hint: the timeout copy used to read "Try a simpler search
# term", and a model that believed it spent eight calls rewording one query,
# every attempt waiting out two stacked timeouts (issue #1496).
_BACKEND_RETRY_HINT = (
    "The failure is not caused by the search term, so rewording will not help. "
    "One retry is worth it, since a single failed call is often a blip. If it "
    "fails again, tell the user pricing lookup is unavailable right now."
)

_OUTAGE_HINT = (
    "Pricing lookup has failed repeatedly in the last minute, so no request was "
    "sent. Retrying and rewording will both fail. Tell the user the lookup is "
    "unavailable right now."
)


class SupplierSearchParams(BaseModel):
    query: str = Field(description="Product search term, e.g. '3/4 plywood' or 'Kilz primer'")
    zip_code: str = Field(default="", description="5-digit US zip code for local pricing")


def _format_results(results: list[ProductResult], query: str, zip_code: str) -> str:
    """Format product results as plain text suitable for SMS/iMessage."""
    if not results:
        return f'No products found for "{query}" at Home Depot.'

    header = f'Found {len(results)} result(s) for "{query}" at Home Depot'
    if zip_code:
        header += f" (zip {zip_code})"
    has_any_price = any(p.price_dollars is not None for p in results)
    if not has_any_price:
        header += " (pricing not available online, check link or call store)"
    lines = [f"{header}:\n"]

    for i, p in enumerate(results, 1):
        # Build the price/size suffix
        size_parts: list[str] = []
        if p.price_dollars is not None:
            price_str = f"${p.price_dollars:.2f}"
            if p.was_price_dollars is not None and p.was_price_dollars > p.price_dollars:
                price_str += f" (was ${p.was_price_dollars:.2f})"
            size_parts.append(price_str)
        if p.unit and p.unit != "each":
            size_parts.append(p.unit)

        name_line = f"{i}. {p.name}"
        if size_parts:
            name_line += f" | {' / '.join(size_parts)}"

        parts = []
        if p.brand:
            parts.append(f"Brand: {p.brand}")
        if p.in_stock is not None:
            stock = "In stock" if p.in_stock else "Out of stock"
            if p.in_stock and p.stock_quantity:
                stock += f" ({p.stock_quantity})"
            parts.append(stock)

        lines.append(name_line)
        if parts:
            lines.append(f"   {' | '.join(parts)}")
        # Always show the product URL so the user can purchase.
        url = p.product_url or "(no link available)"
        lines.append(f"   Link: {url}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _create_pricing_tools(
    supplier: HomeDepotSupplier,
    cache: SupplierCache,
    outages: SupplierCache,
) -> list[Tool]:
    """Build the pricing tool list.

    ``outages`` counts consecutive search failures per zip so a backend that is
    already down is not asked again on every reworded query. It is passed in
    rather than built here because the factory runs per tool context, and a
    per-context counter would never see a streak.
    """

    async def supplier_search_products(query: str, zip_code: str = "") -> ToolResult:
        resolved_zip = zip_code.strip()
        if not resolved_zip:
            return ToolResult(
                content="A zip code is required to look up local pricing.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint=(
                    "Ask the user for their zip code. Once they provide it, "
                    "save it to their USER.md file for future lookups, "
                    "then call this tool again with the zip_code parameter."
                ),
            )

        cache_key = SupplierCache.make_key("home_depot", query, resolved_zip)
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_results(cached, query, resolved_zip))

        outage_key = f"home_depot:{resolved_zip}"
        failures: int = await outages.get(outage_key) or 0
        if failures >= _OUTAGE_STREAK_BEFORE_SUPPRESSING:
            logger.warning(
                "Home Depot search suppressed after %d consecutive failures: query=%r zip=%s",
                failures,
                query,
                resolved_zip,
            )
            return ToolResult(
                content="Home Depot pricing is still down, so this lookup was not sent.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_OUTAGE_HINT,
            )

        try:
            location = Location(zip_code=resolved_zip)
            results = await supplier.search_products(query, location, max_results=5)
        except SupplierUnavailableError as exc:
            await outages.set(outage_key, failures + 1)
            logger.warning("Home Depot refused the search: query=%r reason=%s", query, exc)
            return ToolResult(
                content="Couldn't reach Home Depot to look up pricing.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_BACKEND_RETRY_HINT,
            )
        except httpx.TimeoutException:
            # The search term is not what timed out, so the hint has to match the
            # branch above rather than send the model off rewording.
            await outages.set(outage_key, failures + 1)
            logger.warning("Home Depot search timed out: query=%r zip=%s", query, resolved_zip)
            return ToolResult(
                content="Home Depot pricing timed out.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=_BACKEND_RETRY_HINT,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                logger.error("SerpApi auth failed (401)")
                return ToolResult(
                    content="Supplier pricing is not configured correctly. Contact admin.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            if status == 429:
                return ToolResult(
                    content="Home Depot pricing is temporarily busy. Try again in a moment.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            logger.error("SerpApi error %d for query=%r", status, query)
            return ToolResult(
                content="Couldn't reach Home Depot pricing. Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception:
            logger.exception("Unexpected error in Home Depot search: query=%r", query)
            return ToolResult(
                content="Got an unexpected error looking up pricing. Try again.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        await cache.set(cache_key, results)
        # The streak counts consecutive failures, so an answer ends it.
        if failures:
            await outages.set(outage_key, 0)
        return ToolResult(content=_format_results(results, query, resolved_zip))

    return [
        Tool(
            name=ToolName.SUPPLIER_SEARCH_PRODUCTS,
            description=(
                "Search for products at Home Depot by keyword. "
                "Returns product names, prices, stock, and links. "
                "A zip_code is required for local pricing. Check the user's profile "
                "(USER.md) for a stored zip code before asking."
            ),
            function=supplier_search_products,
            params_model=SupplierSearchParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: f'Search Home Depot for "{args.get("query", "")}"',
            ),
        ),
    ]


def _pricing_factory(ctx: ToolContext) -> list[Tool]:
    """Factory called by the tool registry."""
    if not settings.serpapi_api_key:
        logger.info("supplier_pricing factory: no SERPAPI_API_KEY set, skipping")
        return []

    logger.info("supplier_pricing factory: creating supplier tools (serpapi)")
    return _create_pricing_tools(
        HomeDepotSupplier(api_key=settings.serpapi_api_key), _cache, _outages
    )


async def _pricing_auth_check(ctx: ToolContext) -> str | None:
    """Auth check for the registry. Returns None when ready."""
    return None


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    logger.info("Registering supplier_pricing tool factory")
    default_registry.register(
        "supplier_pricing",
        _pricing_factory,
        core=False,
        summary="Search product prices at Home Depot",
        display_name="Supplier pricing",
        dashboard_description="Search product prices at Home Depot",
        dashboard_group="Integrations",
        dashboard_group_order=3,
        sub_tools=[
            SubToolInfo(
                ToolName.SUPPLIER_SEARCH_PRODUCTS,
                "Search products by keyword at Home Depot",
                default_permission="always",
            ),
        ],
        auth_check=_pricing_auth_check,
    )


_register()
