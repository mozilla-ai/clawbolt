"""Home Depot product search via SerpApi."""

import asyncio
import contextlib
import logging

import httpx

from backend.app.integrations.supplier_pricing.protocol import Location, ProductResult

logger = logging.getLogger(__name__)

_SERPAPI_BASE = "https://serpapi.com/search"


class HomeDepotSupplier:
    """Home Depot product search via SerpApi's dedicated HD engine.

    Requires a SERPAPI_API_KEY. Free tier: 250 searches/month.
    https://serpapi.com/home-depot-search-api
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.name = "homedepot"
        self.display_name = "Home Depot"

    async def _request(self, params: dict[str, str]) -> dict:
        """GET from SerpApi with one retry on 5xx.

        API key is a query param but we never log the full URL.
        """
        full_params = {"api_key": self.api_key, "engine": "home_depot", **params}
        async with httpx.AsyncClient(timeout=20.0) as client:
            for attempt in range(2):
                resp = await client.get(_SERPAPI_BASE, params=full_params)
                if resp.status_code == 429 and attempt == 0:
                    logger.warning("SerpApi rate limited, retrying")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code >= 500 and attempt == 0:
                    logger.warning("SerpApi server error %d, retrying", resp.status_code)
                    await asyncio.sleep(1.0)
                    continue
                resp.raise_for_status()
                return resp.json()
        return {}

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]:
        data = await self._request(
            {
                "q": query,
                "delivery_zip": location.zip_code,
                "ps": str(max_results),
            }
        )

        if data.get("error"):
            logger.warning("SerpApi error for query=%r: %s", query, data["error"])
            return []

        results: list[ProductResult] = []
        for product in (data.get("products") or [])[:max_results]:
            price = product.get("price")
            price_dollars = None
            if isinstance(price, (int, float)):
                price_dollars = float(price)
            elif isinstance(price, str):
                cleaned = price.replace("$", "").replace(",", "").strip()
                with contextlib.suppress(ValueError):
                    price_dollars = float(cleaned)

            was_price = product.get("previous_price") or product.get("old_price")
            was_dollars = None
            if isinstance(was_price, (int, float)):
                was_dollars = float(was_price)
            elif isinstance(was_price, str):
                cleaned = was_price.replace("$", "").replace(",", "").strip()
                with contextlib.suppress(ValueError):
                    was_dollars = float(cleaned)

            delivery = product.get("delivery") or {}
            in_stock = None
            if delivery.get("free_delivery") is not None or delivery.get("has_delivery"):
                in_stock = True

            results.append(
                ProductResult(
                    supplier="homedepot",
                    product_id=str(product.get("product_id", "")),
                    name=product.get("title", "Unknown product"),
                    brand=product.get("brand", ""),
                    price_dollars=price_dollars,
                    was_price_dollars=was_dollars,
                    in_stock=in_stock,
                    aisle="",
                    product_url=product.get("link", ""),
                    image_url=product.get("thumbnail", ""),
                    rating=product.get("rating"),
                )
            )
        return results
