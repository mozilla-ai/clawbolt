"""Home Depot product search via the browser sidecar.

Home Depot's bot manager refuses plain HTTP clients on every product route, so
the only way to reach real product data is to have a real browser issue the
request. ``sidecar/home_depot/`` is that browser, wrapped in a small HTTP API.
This module is the client for it.

The sidecar is ordinary infrastructure from this side: our own service, plain
JSON, no bot protection, so ``httpx`` is the right tool here (unlike
``homedepot_direct``, which needs TLS impersonation). Run the sidecar wherever
a browser can live; it does not have to be the same host as Clawbolt.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.app.integrations.supplier_pricing.homedepot_direct import HomeDepotBlockedError
from backend.app.integrations.supplier_pricing.protocol import Location, ProductResult

logger = logging.getLogger(__name__)


class HomeDepotSidecarSupplier:
    """Home Depot search delegated to the browser sidecar."""

    def __init__(self, base_url: str, *, token: str = "", timeout_seconds: float = 60.0) -> None:
        self.name = "homedepot"
        self.display_name = "Home Depot"
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}"} if self._token else {}

    async def healthy(self) -> bool:
        """Report whether the sidecar is up and its browser is warm."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._base_url}/health", headers=self._headers)
                return resp.status_code == 200 and bool(resp.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]:
        """Search Home Depot through the sidecar.

        Raises:
            HomeDepotBlockedError: the sidecar is unreachable or errored, so the
                caller can fall back to another backend exactly as it would for
                a direct block.
        """
        params = {
            "q": query,
            "zip": location.zip_code,
            "store_id": location.store_id,
            "limit": str(max_results),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/search", params=params, headers=self._headers
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as exc:
            raise HomeDepotBlockedError(
                f"Home Depot sidecar returned {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise HomeDepotBlockedError(f"Home Depot sidecar is unreachable: {exc}") from exc

        products = payload.get("products") or []
        return [_to_result(p) for p in products[:max_results]]


def _to_result(raw: dict[str, Any]) -> ProductResult:
    """Map a sidecar product onto the shared :class:`ProductResult`."""
    return ProductResult(
        supplier="homedepot",
        product_id=str(raw.get("item_id") or ""),
        name=raw.get("name") or "Unknown product",
        brand=raw.get("brand") or "",
        price_dollars=raw.get("price_dollars"),
        was_price_dollars=raw.get("was_price_dollars"),
        in_stock=raw.get("in_stock"),
        stock_quantity=raw.get("stock_quantity"),
        product_url=raw.get("product_url") or "",
        image_url=raw.get("image_url") or "",
        rating=raw.get("rating"),
    )
