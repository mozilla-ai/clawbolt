"""Supplier pricing specialist tools.

Home Depot product search and store lookup. The default backend queries Home
Depot's own endpoints directly and needs no API key; SerpApi is kept as an
optional fallback for the product search, which Home Depot's bot protection can
refuse depending on the outbound IP.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.supplier_pricing.cache import SupplierCache
from backend.app.integrations.supplier_pricing.homedepot import HomeDepotSupplier
from backend.app.integrations.supplier_pricing.homedepot_direct import (
    HomeDepotBlockedError,
    HomeDepotDirectSupplier,
    StoreResult,
)
from backend.app.integrations.supplier_pricing.homedepot_sidecar import HomeDepotSidecarSupplier
from backend.app.integrations.supplier_pricing.protocol import Location, ProductResult

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Module-level singletons shared across all users. The direct supplier holds a
# warmed cookie session, so reusing one instance avoids re-warming per call.
_cache = SupplierCache()
_direct_supplier = HomeDepotDirectSupplier()


class SupplierSearchParams(BaseModel):
    query: str = Field(description="Product search term, e.g. '3/4 plywood' or 'Kilz primer'")
    zip_code: str = Field(default="", description="5-digit US zip code for local pricing")
    store_id: str = Field(
        default="",
        description=(
            "Home Depot store number for store-specific pricing and shelf stock. "
            "Get one from supplier_find_stores. Optional."
        ),
    )


class SupplierFindStoresParams(BaseModel):
    near: str = Field(description="Zip code, city and state, or street address to search near")
    radius_miles: int = Field(default=25, ge=1, le=100, description="Search radius in miles")


def _format_results(
    results: list[ProductResult], query: str, zip_code: str, supplier_name: str = "Home Depot"
) -> str:
    """Format product results as plain text suitable for SMS/iMessage."""
    if not results:
        return f'No products found for "{query}" at {supplier_name}.'

    header = f'Found {len(results)} result(s) for "{query}" at {supplier_name}'
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


def _format_stores(stores: list[StoreResult], near: str) -> str:
    """Format store results as plain text suitable for SMS/iMessage."""
    if not stores:
        return f'No Home Depot stores found near "{near}".'

    lines = [f'Found {len(stores)} Home Depot store(s) near "{near}":\n']
    for i, s in enumerate(stores, 1):
        headline = f"{i}. {s.name} (store #{s.store_id})"
        if s.distance_miles is not None:
            headline += f" | {s.distance_miles} mi"
        lines.append(headline)
        address = ", ".join(part for part in (s.street, s.city, s.state) if part)
        if s.zip_code:
            address = f"{address} {s.zip_code}".strip()
        if address:
            lines.append(f"   {address}")
        if s.phone:
            lines.append(f"   {s.phone}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _create_pricing_tools(
    direct: HomeDepotDirectSupplier | None,
    fallback: HomeDepotSupplier | None,
    cache: SupplierCache,
    sidecar: HomeDepotSidecarSupplier | None = None,
) -> list[Tool]:
    """Build the pricing tool list.

    Product search tries each backend in turn and takes the first that answers:
    ``sidecar`` (a real browser, the only one Home Depot reliably serves), then
    ``direct`` (keyless but bot-walled on product routes), then ``fallback``
    (SerpApi, needs a key). The store lookup is direct-only, since neither of
    the others has an equivalent endpoint and the direct one is never blocked.
    """

    async def _search_with_fallback(
        query: str, location: Location, max_results: int
    ) -> list[ProductResult]:
        """Try each configured backend in order, on to the next when blocked."""
        chain: list[tuple[str, Any]] = [
            (name, backend)
            for name, backend in (
                ("sidecar", sidecar),
                ("direct", direct),
                ("serpapi", fallback),
            )
            if backend is not None
        ]
        if not chain:
            raise HomeDepotBlockedError("No Home Depot backend is configured")

        for index, (name, backend) in enumerate(chain):
            try:
                return await backend.search_products(query, location, max_results=max_results)
            except HomeDepotBlockedError:
                is_last = index == len(chain) - 1
                if is_last:
                    raise
                logger.info(
                    "Home Depot %s backend unavailable, trying %s", name, chain[index + 1][0]
                )
        raise HomeDepotBlockedError("No Home Depot backend answered")

    async def supplier_search_products(
        query: str, zip_code: str = "", store_id: str = ""
    ) -> ToolResult:
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

        resolved_store = store_id.strip()
        cache_key = SupplierCache.make_key(
            "homedepot",
            query,
            f"{resolved_zip}:{resolved_store}" if resolved_store else resolved_zip,
        )
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_results(cached, query, resolved_zip))

        try:
            location = Location(zip_code=resolved_zip, store_id=resolved_store)
            results = await _search_with_fallback(query, location, 5)
        except HomeDepotBlockedError:
            logger.warning("Home Depot refused the search: query=%r", query)
            return ToolResult(
                content=(
                    "Home Depot blocked the price lookup. Their store locator still "
                    "works, so I can find a nearby store to call."
                ),
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=(
                    "Do not retry this query; the block is not caused by the search "
                    "term. Offer supplier_find_stores so the user can phone the store."
                ),
            )
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

    async def supplier_find_stores(near: str, radius_miles: int = 25) -> ToolResult:
        resolved_near = near.strip()
        if not resolved_near:
            return ToolResult(
                content="A zip code, city, or address is required to find stores.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Ask the user where they want to look, then call this tool again.",
            )
        if direct is None:
            return ToolResult(
                content="Store lookup is not available.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        cache_key = SupplierCache.make_key("homedepot_stores", resolved_near, str(radius_miles))
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_stores(cached, resolved_near))

        try:
            stores = await direct.find_stores(resolved_near, radius_miles=radius_miles)
        except HomeDepotBlockedError:
            logger.warning("Home Depot refused the store lookup: near=%r", resolved_near)
            return ToolResult(
                content="Couldn't reach Home Depot's store locator. Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception:
            logger.exception("Unexpected error in Home Depot store lookup: near=%r", resolved_near)
            return ToolResult(
                content="Got an unexpected error looking up stores. Try again.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        await cache.set(cache_key, stores)
        return ToolResult(content=_format_stores(stores, resolved_near))

    return [
        Tool(
            name=ToolName.SUPPLIER_SEARCH_PRODUCTS,
            description=(
                "Search for products at Home Depot by keyword. "
                "Returns product names, prices, stock, and links. "
                "A zip_code is required for local pricing. Check the user's profile "
                "(USER.md) for a stored zip code before asking. Pass store_id as well "
                "when you know it to get that store's price and shelf count."
            ),
            function=supplier_search_products,
            params_model=SupplierSearchParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: f'Search Home Depot for "{args.get("query", "")}"',
            ),
        ),
        Tool(
            name=ToolName.SUPPLIER_FIND_STORES,
            description=(
                "Find Home Depot stores near a zip code, city, or address. "
                "Returns each store's number, address, phone, and distance. "
                "Use the store number with supplier_search_products for "
                "store-specific pricing and shelf stock."
            ),
            function=supplier_find_stores,
            params_model=SupplierFindStoresParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: (
                    f'Find Home Depot stores near "{args.get("near", "")}"'
                ),
            ),
        ),
    ]


def _pricing_factory(ctx: ToolContext) -> list[Tool]:
    """Factory called by the tool registry."""
    sidecar = (
        HomeDepotSidecarSupplier(
            settings.home_depot_sidecar_url, token=settings.home_depot_sidecar_token
        )
        if settings.home_depot_sidecar_url
        else None
    )
    direct = _direct_supplier if settings.supplier_direct_enabled else None
    fallback = (
        HomeDepotSupplier(api_key=settings.serpapi_api_key) if settings.serpapi_api_key else None
    )

    if sidecar is None and direct is None and fallback is None:
        logger.info("supplier_pricing factory: no Home Depot backend configured, skipping")
        return []

    logger.info(
        "supplier_pricing factory: creating Home Depot tools (sidecar=%s, direct=%s, serpapi=%s)",
        sidecar is not None,
        direct is not None,
        fallback is not None,
    )
    return _create_pricing_tools(direct, fallback, _cache, sidecar)


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
        summary="Search product prices and find stores at Home Depot",
        display_name="Home Depot pricing",
        dashboard_description="Search product prices and find stores at Home Depot",
        dashboard_group="Integrations",
        dashboard_group_order=3,
        sub_tools=[
            SubToolInfo(
                ToolName.SUPPLIER_SEARCH_PRODUCTS,
                "Search products by keyword at Home Depot",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.SUPPLIER_FIND_STORES,
                "Find Home Depot stores near a location",
                default_permission="always",
            ),
        ],
        auth_check=_pricing_auth_check,
    )


_register()
