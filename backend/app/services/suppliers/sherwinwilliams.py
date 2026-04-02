"""Sherwin-Williams product search via their WCS REST API and GetPricingService.

No API key required. Uses the public-facing WebSphere Commerce endpoints:
- /wcs/resources/store/{storeId}/productview/ for product catalog data
- /GetPricingService for per-SKU pricing (returns HTML fragments)

Extraction pipeline:
1. Search products by keyword via WCS REST search API
2. Fetch SKU details (size, sheen, base) via WCS REST product view
3. Batch-fetch prices via GetPricingService, parse HTML response
"""

import asyncio
import contextlib
import logging
import re
from html.parser import HTMLParser

import httpx

from backend.app.services.suppliers.protocol import Location, ProductResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.sherwin-williams.com"
_STORE_ID = "10151"
_CATALOG_ID = "11051"
_LANG_ID = "-1"

# Batch size for GetPricingService calls (max ~10 per request).
_PRICE_BATCH_SIZE = 10


class _PriceHTMLParser(HTMLParser):
    """Extract ProductInfoPrice and unitLabel values from GetPricingService HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.prices: dict[str, str] = {}
        self.units: dict[str, str] = {}
        self._current_unit_id: str | None = None
        self._current_unit_text: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        el_id = attr_dict.get("id") or ""

        # <input type="hidden" id="ProductInfoPrice_{skuId}" value="$80.99"/>
        if tag == "input" and el_id.startswith("ProductInfoPrice_"):
            sku_id = el_id.removeprefix("ProductInfoPrice_")
            value = attr_dict.get("value", "")
            if value and sku_id not in self.prices:
                self.prices[sku_id] = value

        # <span id="unitLabel_{skuId}">/ Gallon</span>
        if tag == "span" and el_id.startswith("unitLabel_"):
            self._current_unit_id = el_id.removeprefix("unitLabel_")
            self._current_unit_text = ""

    def handle_data(self, data: str) -> None:
        if self._current_unit_id is not None:
            self._current_unit_text += data

    def handle_endtag(self, tag: str) -> None:
        if self._current_unit_id is not None and tag == "span":
            text = self._current_unit_text.strip().lstrip("/").strip()
            if text:
                self.units[self._current_unit_id] = text
            self._current_unit_id = None


def _parse_price(price_str: str) -> float | None:
    """Parse '$80.99' or '$80.99 - $85.99' into a float (uses the low end)."""
    if not price_str:
        return None
    # Take the first price if it's a range
    first = price_str.split("-")[0].strip()
    cleaned = first.replace("$", "").replace(",", "").strip()
    with contextlib.suppress(ValueError):
        return float(cleaned)
    return None


class SherwinWilliamsSupplier:
    """Sherwin-Williams product search via public WCS REST API.

    No API key required. Hits the same endpoints the sherwin-williams.com
    website uses: WCS REST for catalog data, GetPricingService for prices.
    """

    def __init__(self) -> None:
        self.name = "sherwinwilliams"
        self.display_name = "Sherwin-Williams"

    async def _request(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        """GET from sherwin-williams.com with retry on 5xx/429."""
        url = f"{_BASE_URL}{path}"
        headers = {
            "Accept": "application/json, text/html, */*",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for attempt in range(2):
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 429 and attempt == 0:
                    logger.warning("SW rate limited, retrying")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code >= 500 and attempt == 0:
                    logger.warning("SW server error %d, retrying", resp.status_code)
                    await asyncio.sleep(1.0)
                    continue
                resp.raise_for_status()
                return resp
        return resp

    async def _search_catalog(self, query: str, max_results: int) -> list[dict]:
        """Step 1: Search products via WCS REST search API."""
        resp = await self._request(
            f"/wcs/resources/store/{_STORE_ID}/productview/bySearchTerm/{query}",
            params={
                "langId": _LANG_ID,
                "responseFormat": "json",
                "pageSize": str(max_results),
            },
        )
        data = resp.json()
        return data.get("CatalogEntryView", [])

    async def _get_product_skus(self, product_id: str) -> list[dict]:
        """Step 2: Fetch SKU details (size, sheen, base) for a product."""
        resp = await self._request(
            f"/wcs/resources/store/{_STORE_ID}/productview/byId/{product_id}",
            params={"langId": _LANG_ID, "responseFormat": "json"},
        )
        data = resp.json()
        entries = data.get("CatalogEntryView", [])
        if not entries:
            return []
        return entries[0].get("SKUs", [])

    async def _fetch_prices(self, sku_ids: list[str]) -> dict[str, str]:
        """Step 3: Batch-fetch prices via GetPricingService (returns HTML).

        Returns {sku_id: price_string} e.g. {"16329": "$80.99"}.
        """
        if not sku_ids:
            return {}

        all_prices: dict[str, str] = {}

        for batch_start in range(0, len(sku_ids), _PRICE_BATCH_SIZE):
            batch = sku_ids[batch_start : batch_start + _PRICE_BATCH_SIZE]
            params: dict[str, str] = {
                "isPDP": "true",
                "isPLP": "false",
                "catalogId": _CATALOG_ID,
                "storeId": _STORE_ID,
                "langId": _LANG_ID,
                "hasProducts": "true",
                "ids": ",".join(batch) + ",",
            }
            for i, sku_id in enumerate(batch, 1):
                params[f"cId_{i}"] = sku_id
                params[f"qty_{i}"] = "1"

            resp = await self._request("/GetPricingService", params=params)
            parser = _PriceHTMLParser()
            parser.feed(resp.text)
            all_prices.update(parser.prices)

            if batch_start + _PRICE_BATCH_SIZE < len(sku_ids):
                await asyncio.sleep(0.5)

        return all_prices

    def _extract_sku_attr(self, sku: dict, attr_id: str) -> str:
        """Pull a defining attribute value from a SKU's attribute list."""
        for attr in sku.get("Attributes", []):
            if attr.get("identifier") == attr_id:
                values = attr.get("Values", [])
                if values:
                    return values[0].get("values", "")
        return ""

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]:
        """Search Sherwin-Williams for products, returning priced results.

        Pipeline: search catalog -> fetch SKUs -> batch price lookup -> combine.
        """
        catalog_entries = await self._search_catalog(query, max_results)
        if not catalog_entries:
            return []

        results: list[ProductResult] = []

        for entry in catalog_entries[:max_results]:
            product_id = entry.get("uniqueID", "")
            product_name = entry.get("name", "Unknown product")
            product_url = f"{_BASE_URL}/homeowners/products/{_slugify(product_name)}"

            # Get SKUs for this product
            try:
                skus = await self._get_product_skus(product_id)
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                logger.warning("Failed to fetch SKUs for product %s", product_id)
                skus = []

            if not skus:
                # No SKUs: return product-level entry without per-SKU pricing
                results.append(
                    ProductResult(
                        supplier="sherwinwilliams",
                        product_id=product_id,
                        name=product_name,
                        brand="Sherwin-Williams",
                        product_url=product_url,
                    )
                )
                continue

            # Fetch prices for all SKUs of this product
            sku_ids = [s["SKUUniqueID"] for s in skus if "SKUUniqueID" in s]
            try:
                prices = await self._fetch_prices(sku_ids)
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                logger.warning("Failed to fetch prices for product %s", product_id)
                prices = {}

            # Group SKUs by size to find representative prices
            seen_sizes: set[str] = set()
            for sku in skus:
                sku_id = sku.get("SKUUniqueID", "")
                size = self._extract_sku_attr(sku, "ATT_calc_size__volume_or_weight_+_item_")

                # Skip if we already have this size (one per size is enough for search)
                if size in seen_sizes:
                    continue
                seen_sizes.add(size)

                price_str = prices.get(sku_id, "")
                price_dollars = _parse_price(price_str)

                unit = "each"
                if "gallon" in size.lower() or "quart" in size.lower():
                    unit = size.lower()

                sku_name = product_name
                if size:
                    sku_name = f"{product_name} ({size})"

                results.append(
                    ProductResult(
                        supplier="sherwinwilliams",
                        product_id=sku_id or product_id,
                        name=sku_name,
                        brand="Sherwin-Williams",
                        price_dollars=price_dollars,
                        unit=unit,
                        product_url=product_url,
                    )
                )

        return results


def _slugify(name: str) -> str:
    """Convert product name to URL slug: 'SuperPaint Interior' -> 'superpaint-interior'."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")
