"""Lowe's site adapter for the retail sidecar.

Lowe's differs from Home Depot in two ways that matter, both measured rather than
assumed.

**Warming needs a real click.** A homepage visit alone is not enough: navigating
to /search from a session warmed only by the homepage is refused with a 403
Akamai edge deny ("You don't have permission to access ... on this server"). One
organic click into a category first, and the same navigation returns results.
Freshly created profiles are denied; profiles warmed this way are served.

**There is no product JSON API to call.** Home Depot exposes a GraphQL gateway
that can be fetched from inside the page. Lowe's equivalent, /store/api/search,
is denied by the same edge rule, so results have to come from the search page
itself. They do not have to come from the DOM though: the page embeds the whole
result set in ``window['__PRELOADED_STATE__']``, which carries price, per-store
on-hand quantity, brand, model, rating and review count. Parsing that is far
sturdier than selectors, and it also avoids the "Previously Viewed" carousel that
otherwise contaminates a DOM scrape with items from earlier queries.
"""

from __future__ import annotations

import json
import re
from typing import Any

ORIGIN = "https://www.lowes.com"

# The state assignment as the page emits it. Anchored on the bracket form
# because that is what Lowe's ships; a dotted form would not match.
_STATE_RE = re.compile(r"window\['__PRELOADED_STATE__'\]\s*=\s*")


def extract_preloaded_state(html: str) -> dict[str, Any] | None:
    """Pull ``window['__PRELOADED_STATE__']`` out of a page.

    Brace-matched rather than regex-terminated: the payload is ~400KB of nested
    JSON containing braces inside strings, so a greedy or lazy pattern gets the
    boundary wrong. Returns None when the marker is absent or the slice does not
    parse, which is the caller's signal that the page was not a result page.
    """
    match = _STATE_RE.search(html)
    if match is None:
        return None

    start = match.end()
    depth = 0
    in_string = False
    escaped = False
    index = start

    while index < len(html):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                index += 1
                break
        index += 1

    try:
        parsed = json.loads(html[start:index])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _stock_from_inventory(location: dict[str, Any]) -> tuple[bool | None, int | None]:
    """Reduce Lowe's per-fulfilment availability to (in_stock, quantity).

    ``itemAvailList`` carries one entry per fulfilment method (Parcel, Pickup,
    ...), each with its own availability flag and on-hand count. Treat the item
    as in stock if any method is available, and report the largest on-hand seen,
    which is the local store's shelf count.
    """
    entries = (location.get("itemInventory") or {}).get("itemAvailList") or []
    if not entries:
        return None, None

    in_stock = False
    quantity: int | None = None
    for entry in entries:
        if entry.get("isAvlSts"):
            in_stock = True
        on_hand = entry.get("onhandQty")
        if isinstance(on_hand, int) and (quantity is None or on_hand > quantity):
            quantity = on_hand
    return in_stock, quantity


def parse_products(state: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Map ``itemList`` entries onto the sidecar's product shape."""
    products: list[dict[str, Any]] = []

    for entry in (state.get("itemList") or [])[:limit]:
        product = entry.get("product") or {}
        location = entry.get("location") or {}

        price = (location.get("price") or {}).get("sellingPrice")
        in_stock, quantity = _stock_from_inventory(location)

        detail_path = product.get("pdURL") or ""
        url = f"{ORIGIN}{detail_path}" if detail_path.startswith("/") else detail_path

        products.append(
            {
                "item_id": str(product.get("omniItemId") or ""),
                "name": product.get("description") or "Unknown product",
                "brand": product.get("brand") or "",
                "model_number": str(product.get("modelId") or ""),
                "price_dollars": float(price) if isinstance(price, (int, float)) else None,
                "was_price_dollars": None,
                "in_stock": in_stock,
                "stock_quantity": quantity,
                "rating": product.get("rating"),
                "review_count": product.get("reviewCount"),
                "product_url": url,
                "image_url": product.get("imageUrl") or "",
            }
        )

    return products


def search_url(keyword: str, store_id: str = "") -> str:
    """Build the search URL. Store selection rides on the session, not the URL."""
    from urllib.parse import quote_plus

    return f"{ORIGIN}/search?searchTerm={quote_plus(keyword)}"


def is_denied(html: str) -> bool:
    """True when the edge refused the request rather than serving a page."""
    return "Access Denied" in html
