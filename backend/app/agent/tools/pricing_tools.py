"""Supplier pricing specialist tools.

Phase 1a: supplier_search_products backed by Traject Data Backyard API.
"""

import logging

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.services.suppliers.backyard import BackyardSupplier
from backend.app.services.suppliers.cache import SupplierCache
from backend.app.services.suppliers.protocol import Location

logger = logging.getLogger(__name__)

_VALID_STORES = ("homedepot", "lowes")
_STORE_DISPLAY = {"homedepot": "Home Depot", "lowes": "Lowe's"}

# Module-level cache singleton shared across all users.
_cache = SupplierCache()


class SupplierSearchParams(BaseModel):
    query: str = Field(description="Product search term, e.g. '3/4 plywood' or 'Kilz primer'")
    store: str = Field(description="Which store to search: 'homedepot' or 'lowes'")
    zip_code: str = Field(default="", description="5-digit US zip code for local pricing")


def _format_results(results: list, query: str, store: str, zip_code: str) -> str:
    """Format product results as plain text suitable for SMS/iMessage."""
    display = _STORE_DISPLAY.get(store, store)
    if not results:
        return f'No products found for "{query}" at {display} near {zip_code}.'

    lines = [f'Found {len(results)} result(s) for "{query}" at {display} (zip {zip_code}):\n']
    for i, p in enumerate(results, 1):
        price_str = (
            f"${p.price_dollars:.2f}" if p.price_dollars is not None else "Price unavailable"
        )
        if p.was_price_dollars is not None and p.price_dollars is not None:
            price_str += f" (was ${p.was_price_dollars:.2f})"

        parts = []
        if p.brand:
            parts.append(f"Brand: {p.brand}")
        if p.in_stock is not None:
            stock = "In stock"
            if p.stock_quantity is not None:
                stock += f" ({p.stock_quantity})"
            if not p.in_stock:
                stock = "Out of stock"
            parts.append(stock)
        if p.aisle:
            parts.append(f"Aisle {p.aisle}")

        lines.append(f"{i}. {p.name} | {price_str}")
        if parts:
            lines.append(f"   {' | '.join(parts)}")
        if p.product_url:
            lines.append(f"   {p.product_url}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _create_pricing_tools(
    suppliers: dict[str, BackyardSupplier],
    cache: SupplierCache,
) -> list[Tool]:
    """Build the pricing tool list. Captures suppliers and cache via closure."""

    async def supplier_search_products(query: str, store: str, zip_code: str = "") -> ToolResult:
        if store not in _VALID_STORES:
            return ToolResult(
                content=f"Store must be 'homedepot' or 'lowes', got '{store}'.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

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

        supplier = suppliers[store]
        cache_key = SupplierCache.make_key(supplier.name, query, resolved_zip)
        cached = await cache.get(cache_key)
        if cached is not None:
            return ToolResult(content=_format_results(cached, query, store, resolved_zip))

        try:
            location = Location(zip_code=resolved_zip)
            results = await supplier.search_products(query, location, max_results=5)
        except httpx.TimeoutException:
            logger.warning("Supplier search timed out: store=%s query=%r", store, query)
            return ToolResult(
                content="The price lookup timed out. Try a simpler search term.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                logger.error("Backyard API auth failed (401) for engine=%s", store)
                return ToolResult(
                    content="Supplier pricing is not configured correctly. Contact admin.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            if status == 429:
                return ToolResult(
                    content="Store pricing is temporarily busy. Try again in a moment.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            display = _STORE_DISPLAY.get(store, store)
            logger.error("Backyard API error %d for engine=%s", status, store)
            return ToolResult(
                content=f"Couldn't reach {display} pricing. Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except Exception:
            logger.exception("Unexpected error in supplier search: store=%s query=%r", store, query)
            return ToolResult(
                content="Got an unexpected error looking up pricing. Try again.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        await cache.set(cache_key, results)
        return ToolResult(content=_format_results(results, query, store, resolved_zip))

    return [
        Tool(
            name=ToolName.SUPPLIER_SEARCH_PRODUCTS,
            description=(
                "Search for products at Home Depot or Lowe's by keyword. "
                "Returns product names, prices, stock levels, and store locations. "
                "The user must specify which store to search. "
                "A zip_code is required for local pricing. Check the user's profile "
                "(USER.md) for a stored zip code before asking."
            ),
            function=supplier_search_products,
            params_model=SupplierSearchParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
                description_builder=lambda args: (
                    f"Search {_STORE_DISPLAY.get(args.get('store', ''), 'store')}"
                    f' for "{args.get("query", "")}"'
                ),
            ),
        ),
    ]


def _pricing_factory(ctx: "ToolContext") -> list[Tool]:  # noqa: F821
    """Factory called by the tool registry."""
    if not settings.backyard_api_key:
        logger.info("supplier_pricing factory: BACKYARD_API_KEY not set, returning no tools")
        return []
    logger.info(
        "supplier_pricing factory: creating tools (key length=%d)", len(settings.backyard_api_key)
    )
    hd = BackyardSupplier(settings.backyard_api_key, engine="homedepot")
    lowes = BackyardSupplier(settings.backyard_api_key, engine="lowes")
    return _create_pricing_tools({"homedepot": hd, "lowes": lowes}, _cache)


def _pricing_auth_check(ctx: "ToolContext") -> str | None:  # noqa: F821
    """Auth check for the registry.

    Returns None when ready (key is set) or when unconfigured (hides the specialist).
    There is no per-user auth in Phase 1, so this always returns None.
    """
    if not settings.backyard_api_key:
        logger.debug("supplier_pricing auth_check: BACKYARD_API_KEY not set")
        return None
    return None


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    logger.info("Registering supplier_pricing tool factory")
    default_registry.register(
        "supplier_pricing",
        _pricing_factory,
        core=False,
        summary="Search product prices at Home Depot and Lowe's",
        sub_tools=[
            SubToolInfo(
                ToolName.SUPPLIER_SEARCH_PRODUCTS,
                "Search products by keyword at Home Depot or Lowe's",
                default_permission="auto",
            ),
        ],
        auth_check=_pricing_auth_check,
    )


_register()
