"""Traject Data Backyard API client for Home Depot and Lowe's."""

import asyncio
import logging

import httpx

from backend.app.services.suppliers.protocol import Location, ProductResult

logger = logging.getLogger(__name__)

_VALID_ENGINES = ("homedepot", "lowes")


class BackyardSupplier:
    """Backyard API client. One instance per engine (homedepot or lowes)."""

    BASE_URL = "https://api.backyardapi.com/request"

    def __init__(self, api_key: str, engine: str = "homedepot") -> None:
        if engine not in _VALID_ENGINES:
            msg = f"engine must be one of {_VALID_ENGINES}, got {engine!r}"
            raise ValueError(msg)
        self.api_key = api_key
        self.engine = engine
        self.name = f"backyard_{engine}"
        self.display_name = "Home Depot" if engine == "homedepot" else "Lowe's"

    async def _request(self, params: dict[str, str]) -> dict:
        """Make API request with one retry on 429/5xx.

        Never logs the full URL (contains api_key as query param).
        """
        full_params = {"api_key": self.api_key, "engine": self.engine, **params}
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(2):
                resp = await client.get(self.BASE_URL, params=full_params)
                if resp.status_code == 429 and attempt == 0:
                    logger.warning("Backyard API rate limited (%s), retrying", self.engine)
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code >= 500 and attempt == 0:
                    logger.warning(
                        "Backyard API server error %d (%s), retrying",
                        resp.status_code,
                        self.engine,
                    )
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
                "type": "search",
                "search_term": query,
                "customer_zipcode": location.zip_code,
            }
        )
        results: list[ProductResult] = []
        for item in (data.get("search_results") or [])[:max_results]:
            product = item.get("product", {})
            buybox = product.get("buybox_winner") or {}
            fulfillment = buybox.get("fulfillment") or {}
            pickup = fulfillment.get("pickup_info") or {}
            images = product.get("images") or []
            results.append(
                ProductResult(
                    supplier=self.engine,
                    product_id=str(product.get("item_id", "")),
                    name=product.get("title", "Unknown product"),
                    brand=product.get("brand", ""),
                    price_dollars=buybox.get("price"),
                    was_price_dollars=buybox.get("was_price"),
                    in_stock=pickup.get("in_stock"),
                    stock_quantity=pickup.get("stock_level"),
                    aisle=product.get("aisle", ""),
                    product_url=product.get("link", ""),
                    image_url=images[0].get("link", "") if images else "",
                    rating=product.get("rating"),
                )
            )
        return results
