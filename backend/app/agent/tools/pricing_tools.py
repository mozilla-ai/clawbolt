"""Supplier pricing specialist tools.

Phase 1a: supplier_search_products for Home Depot via SerpApi.
Phase 1b: supplier_search_paint for Sherwin-Williams via public WCS REST API.
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
from backend.app.services.suppliers.cache import SupplierCache
from backend.app.services.suppliers.homedepot import HomeDepotSupplier
from backend.app.services.suppliers.protocol import Location, ProductResult
from backend.app.services.suppliers.sherwinwilliams import SherwinWilliamsSupplier

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Module-level cache singleton shared across all users.
_cache = SupplierCache()


class SupplierSearchParams(BaseModel):
    query: str = Field(description="Product search term, e.g. '3/4 plywood' or 'Kilz primer'")
    zip_code: str = Field(default="", description="5-digit US zip code for local pricing")


class PaintSearchParams(BaseModel):
    query: str = Field(
        description="Paint search term, e.g. 'superpaint interior' or 'emerald exterior'"
    )


def _format_results(
    results: list[ProductResult], query: str, zip_code: str, supplier_name: str = "Home Depot"
) -> str:
    """Format product results as plain text suitable for SMS/iMessage."""
    if not results:
        return f'No products found for "{query}" at {supplier_name}.'

    header = f'Found {len(results)} result(s) for "{query}" at {supplier_name}'
    if zip_code:
        header += f" (zip {zip_code})"
    lines = [f"{header}:\n"]

    for i, p in enumerate(results, 1):
        price_str = (
            f"${p.price_dollars:.2f}" if p.price_dollars is not None else "Price unavailable"
        )
        if (
            p.was_price_dollars is not None
            and p.price_dollars is not None
            and p.was_price_dollars > p.price_dollars
        ):
            price_str += f" (was ${p.was_price_dollars:.2f})"

        if p.unit and p.unit != "each":
            price_str += f" / {p.unit}"

        parts = []
        if p.brand:
            parts.append(f"Brand: {p.brand}")
        if p.in_stock is not None:
            stock = "In stock" if p.in_stock else "Out of stock"
            parts.append(stock)

        lines.append(f"{i}. {p.name} | {price_str}")
        if parts:
            lines.append(f"   {' | '.join(parts)}")
        if p.product_url:
            lines.append(f"   {p.product_url}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _create_pricing_tools(
    supplier: HomeDepotSupplier,
    cache: SupplierCache,
) -> list[Tool]:
    """Build the pricing tool list. Captures supplier and cache via closure."""

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

        cache_key = SupplierCache.make_key("homedepot", query, resolved_zip)
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_results(cached, query, resolved_zip))

        try:
            location = Location(zip_code=resolved_zip)
            results = await supplier.search_products(query, location, max_results=5)
        except httpx.TimeoutException:
            logger.warning("Home Depot search timed out: query=%r zip=%s", query, resolved_zip)
            return ToolResult(
                content="The price lookup timed out. Try a simpler search term.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
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
        return ToolResult(content=_format_results(results, query, resolved_zip))

    return [
        Tool(
            name=ToolName.SUPPLIER_SEARCH_PRODUCTS,
            description=(
                "Search for products at Home Depot by keyword. "
                "Returns product names, prices, and links. "
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


def _create_paint_tools(
    supplier: SherwinWilliamsSupplier,
    cache: SupplierCache,
) -> list[Tool]:
    """Build Sherwin-Williams paint search tools."""

    async def supplier_search_paint(query: str) -> ToolResult:
        cache_key = SupplierCache.make_key("sherwinwilliams", query, "national")
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_results(cached, query, "", "Sherwin-Williams"))

        try:
            location = Location(zip_code="00000")
            results = await supplier.search_products(query, location, max_results=5)
        except httpx.TimeoutException:
            logger.warning("SW search timed out: query=%r", query)
            return ToolResult(
                content="The Sherwin-Williams price lookup timed out. Try a simpler search term.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                return ToolResult(
                    content=(
                        "Sherwin-Williams pricing is temporarily busy. Try again in a moment."
                    ),
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            logger.error("SW error %d for query=%r", status, query)
            return ToolResult(
                content="Couldn't reach Sherwin-Williams pricing. Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception:
            logger.exception("Unexpected error in SW search: query=%r", query)
            return ToolResult(
                content="Got an unexpected error looking up paint pricing. Try again.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        await cache.set(cache_key, results)
        return ToolResult(content=_format_results(results, query, "", "Sherwin-Williams"))

    return [
        Tool(
            name=ToolName.SUPPLIER_SEARCH_PAINT,
            description=(
                "Search for paint products at Sherwin-Williams by keyword. "
                "Returns paint names, prices per size (quart/gallon/5-gallon), and links. "
                "No zip code required; Sherwin-Williams uses national list pricing."
            ),
            function=supplier_search_paint,
            params_model=PaintSearchParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: (
                    f'Search Sherwin-Williams for "{args.get("query", "")}"'
                ),
            ),
        ),
    ]


def _pricing_factory(ctx: ToolContext) -> list[Tool]:
    """Factory called by the tool registry."""
    tools: list[Tool] = []

    if settings.serpapi_api_key:
        logger.info("supplier_pricing factory: creating Home Depot pricing tools")
        hd_supplier = HomeDepotSupplier(api_key=settings.serpapi_api_key)
        tools.extend(_create_pricing_tools(hd_supplier, _cache))
    else:
        logger.info("supplier_pricing factory: SERPAPI_API_KEY not set, skipping Home Depot")

    logger.info("supplier_pricing factory: creating Sherwin-Williams pricing tools")
    sw_supplier = SherwinWilliamsSupplier()
    tools.extend(_create_paint_tools(sw_supplier, _cache))

    return tools


def _pricing_auth_check(ctx: ToolContext) -> str | None:
    """Auth check for the registry.

    Returns None when ready. SW requires no key, so paint tools are always available.
    HD requires SERPAPI_API_KEY but we don't gate the whole factory on it.
    """
    return None


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    logger.info("Registering supplier_pricing tool factory")
    default_registry.register(
        "supplier_pricing",
        _pricing_factory,
        core=False,
        summary="Search product prices at Home Depot and Sherwin-Williams",
        sub_tools=[
            SubToolInfo(
                ToolName.SUPPLIER_SEARCH_PRODUCTS,
                "Search products by keyword at Home Depot",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SUPPLIER_SEARCH_PAINT,
                "Search paint products by keyword at Sherwin-Williams",
                default_permission="always",
            ),
        ],
        auth_check=_pricing_auth_check,
    )


_register()
