"""Supplier pricing specialist tools.

Product search at Home Depot and Lowe's, plus Home Depot store lookup, all served
by the browser sidecar (``sidecar/home_depot/``). Both retailers refuse plain HTTP
clients, so a real browser is the only client either serves.

SerpApi stays available as a fallback for Home Depot product search only: it has
no Lowe's engine, and no store locator.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.supplier_pricing.cache import SupplierCache
from backend.app.integrations.supplier_pricing.errors import SupplierUnavailableError
from backend.app.integrations.supplier_pricing.homedepot import HomeDepotSupplier
from backend.app.integrations.supplier_pricing.protocol import (
    Location,
    ProductResult,
    StoreResult,
)
from backend.app.integrations.supplier_pricing.sidecar_client import SidecarSupplier

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Shared across all users; the sidecar client is stateless so only the cache
# needs to be a singleton.
_cache = SupplierCache()


class SupplierSearchParams(BaseModel):
    query: str = Field(description="Product search term, e.g. '3/4 plywood' or 'Kilz primer'")
    zip_code: str = Field(default="", description="5-digit US zip code for local pricing")
    supplier: Literal["home_depot", "lowes"] = Field(
        default="home_depot",
        description=(
            "Which retailer to search. Call once per retailer to compare prices. "
            "Home Depot supports store_id for store-specific pricing; Lowe's does not."
        ),
    )
    store_id: str = Field(
        default="",
        description=(
            "Home Depot store number for store-specific pricing and shelf stock. "
            "Get one from supplier_find_stores. Ignored for Lowe's. Optional."
        ),
    )


class SupplierFindStoresParams(BaseModel):
    near: str = Field(description="Zip code, city and state, or street address to search near")
    radius_miles: int = Field(default=25, ge=1, le=100, description="Search radius in miles")


def _format_results(
    results: list[ProductResult],
    query: str,
    zip_code: str,
    supplier_name: str = "Home Depot",
    *,
    localized: bool = True,
) -> str:
    """Format product results as plain text suitable for SMS/iMessage.

    ``localized`` says whether the zip actually shaped these results. Only claim
    it when it did: Lowe's results come from whichever store the sidecar's own
    session is pinned to, so labelling them with the user's zip would invite a
    tradesperson to drive to a store on the strength of someone else's shelf
    count.
    """
    if not results:
        return f'No products found for "{query}" at {supplier_name}.'

    header = f'Found {len(results)} result(s) for "{query}" at {supplier_name}'
    if zip_code and localized:
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

    if not localized and any(p.stock_quantity is not None for p in results):
        lines.append(
            "Note: these are not localized to your zip. Stock counts are for "
            f"{supplier_name}'s default store, so confirm before making a trip."
        )

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


# Human-facing retailer names, keyed by the tool's `supplier` argument.
_SUPPLIER_LABELS = {"home_depot": "Home Depot", "lowes": "Lowe's"}


def _create_pricing_tools(
    sidecars: dict[str, SidecarSupplier],
    fallback: HomeDepotSupplier | None,
    cache: SupplierCache,
) -> list[Tool]:
    """Build the pricing tool list.

    ``sidecars`` is keyed by the tool's ``supplier`` argument. Product search
    tries the requested retailer's sidecar and falls through to ``fallback``
    (SerpApi) when it cannot answer, which only helps Home Depot: SerpApi has no
    Lowe's engine. Store lookup is Home Depot sidecar only.
    """

    async def _search_with_fallback(
        supplier: str, query: str, location: Location, max_results: int
    ) -> list[ProductResult]:
        """Try the retailer's sidecar, then SerpApi where it applies."""
        chain: list[tuple[str, Any]] = []
        sidecar = sidecars.get(supplier)
        if sidecar is not None:
            chain.append(("sidecar", sidecar))
        # SerpApi only has a Home Depot engine, so it is not a fallback for Lowe's.
        if fallback is not None and supplier == "home_depot":
            chain.append(("serpapi", fallback))
        if not chain:
            raise SupplierUnavailableError(f"No backend is configured for {supplier}")

        for index, (name, backend) in enumerate(chain):
            try:
                return await backend.search_products(query, location, max_results=max_results)
            except SupplierUnavailableError:
                if index == len(chain) - 1:
                    raise
                logger.info(
                    "%s %s backend unavailable, trying %s", supplier, name, chain[index + 1][0]
                )
        raise SupplierUnavailableError(f"No backend answered for {supplier}")

    async def supplier_search_products(
        query: str, zip_code: str = "", supplier: str = "home_depot", store_id: str = ""
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
        label = _SUPPLIER_LABELS.get(supplier, supplier)
        # Only Home Depot's search takes the zip and store into account; Lowe's
        # ignores both, so keying its cache on them would only cause redundant
        # fetches for answers that cannot differ.
        localized = supplier == "home_depot"
        cache_scope = (
            (f"{resolved_zip}:{resolved_store}" if resolved_store else resolved_zip)
            if localized
            else "national"
        )
        cache_key = SupplierCache.make_key(supplier, query, cache_scope)
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(
                content=_format_results(cached, query, resolved_zip, label, localized=localized)
            )

        try:
            location = Location(zip_code=resolved_zip, store_id=resolved_store)
            results = await _search_with_fallback(supplier, query, location, 5)
        except SupplierUnavailableError:
            logger.warning("%s refused the search: query=%r", label, query)
            return ToolResult(
                content=f"Couldn't reach {label} to look up pricing.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=(
                    "The failure is not caused by the search term, so rewording will "
                    "not help. One retry is worth it: the backend warms a browser "
                    "session lazily and the second attempt often succeeds. If it fails "
                    "again, tell the user the lookup is unavailable and offer the other "
                    "retailer, which may still work."
                ),
            )
        except httpx.TimeoutException:
            logger.warning("%s search timed out: query=%r zip=%s", label, query, resolved_zip)
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
                    content=f"{label} pricing is temporarily busy. Try again in a moment.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            logger.error("SerpApi error %d for query=%r", status, query)
            return ToolResult(
                content=f"Couldn't reach {label} pricing. Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception:
            logger.exception("Unexpected error in %s search: query=%r", label, query)
            return ToolResult(
                content="Got an unexpected error looking up pricing. Try again.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        await cache.set(cache_key, results)
        return ToolResult(
            content=_format_results(results, query, resolved_zip, label, localized=localized)
        )

    async def supplier_find_stores(near: str, radius_miles: int = 25) -> ToolResult:
        resolved_near = near.strip()
        if not resolved_near:
            return ToolResult(
                content="A zip code, city, or address is required to find stores.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Ask the user where they want to look, then call this tool again.",
            )
        hd = sidecars.get("home_depot")
        if hd is None:
            return ToolResult(
                content="Store lookup is not available.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
                hint=(
                    "Store lookup needs the Home Depot sidecar. Tell the user it is "
                    "not configured; do not retry."
                ),
            )

        cache_key = SupplierCache.make_key("homedepot_stores", resolved_near, str(radius_miles))
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_stores(cached, resolved_near))

        try:
            stores = await hd.find_stores(resolved_near, radius_miles=radius_miles)
        except SupplierUnavailableError:
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
                "Search for products at Home Depot or Lowe's by keyword. "
                "Returns product names, prices, stock, and links. "
                "A zip_code is required for local pricing. Check the user's profile "
                "(USER.md) for a stored zip code before asking. Set supplier to pick "
                "the retailer, and call once per retailer when the user wants prices "
                "compared. Pass store_id as well when you know it to get that store's "
                "price and shelf count; Home Depot only."
            ),
            function=supplier_search_products,
            params_model=SupplierSearchParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ALWAYS,
                description_builder=lambda args: (
                    f"Search {_SUPPLIER_LABELS.get(args.get('supplier', 'home_depot'), 'Home Depot')}"
                    f' for "{args.get("query", "")}"'
                ),
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
    sidecars: dict[str, SidecarSupplier] = {}
    if settings.home_depot_sidecar_url:
        # One sidecar process serves both retailers; only the site differs.
        for site, name, label in (
            ("home_depot", "homedepot", "Home Depot"),
            ("lowes", "lowes", "Lowe's"),
        ):
            sidecars[site] = SidecarSupplier(
                settings.home_depot_sidecar_url,
                site=site,
                name=name,
                display_name=label,
                token=settings.home_depot_sidecar_token,
            )

    fallback = (
        HomeDepotSupplier(api_key=settings.serpapi_api_key) if settings.serpapi_api_key else None
    )

    if not sidecars and fallback is None:
        logger.info("supplier_pricing factory: no supplier backend configured, skipping")
        return []

    logger.info(
        "supplier_pricing factory: creating supplier tools (sidecar_sites=%s, serpapi=%s)",
        sorted(sidecars) or "none",
        fallback is not None,
    )
    return _create_pricing_tools(sidecars, fallback, _cache)


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
        summary="Search product prices at Home Depot and Lowe's, and find Home Depot stores",
        display_name="Supplier pricing",
        dashboard_description=(
            "Search product prices at Home Depot and Lowe's, and find Home Depot stores"
        ),
        dashboard_group="Integrations",
        dashboard_group_order=3,
        sub_tools=[
            SubToolInfo(
                ToolName.SUPPLIER_SEARCH_PRODUCTS,
                "Search products by keyword at Home Depot or Lowe's",
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
