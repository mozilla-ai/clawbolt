"""Client for the retail browser sidecar.

Home Depot and Lowe's both refuse plain HTTP clients, so the only way to reach
either is to have a real browser issue the request. ``sidecar/home_depot/`` is
that browser, wrapped in a small HTTP API, and this module is the client for it.

One class serves both retailers because the sidecar hides the differences. Behind
its ``site`` parameter, Home Depot answers a GraphQL call made from inside the
page while Lowe's results are read out of its search page's embedded state, but
both come back in the same shape.

The sidecar is ordinary infrastructure from this side: our own service, plain
JSON, no bot protection, so ``httpx`` is the right tool. Run it wherever a
browser can live; it does not have to share a host with Clawbolt.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.app.integrations.supplier_pricing.errors import SupplierUnavailableError
from backend.app.integrations.supplier_pricing.protocol import (
    Location,
    ProductResult,
    StoreResult,
)

logger = logging.getLogger(__name__)

# Measured p100 for a live search is ~1.3s, so anything beyond a few seconds
# means the sidecar is wedged rather than slow. Fail fast to the next backend
# instead of stalling the user's turn.
_DEFAULT_TIMEOUT_SECONDS = 20.0


class SidecarSupplier:
    """A retailer served by the browser sidecar.

    ``site`` selects the retailer inside the sidecar. Store lookup is Home Depot
    only, since that is the only locator implemented there.
    """

    def __init__(
        self,
        base_url: str,
        *,
        site: str = "home_depot",
        name: str = "homedepot",
        display_name: str = "Home Depot",
        token: str = "",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.site = site
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}"} if self._token else {}

    async def healthy(self) -> bool:
        """Report whether the sidecar is up and its browser is responding."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._base_url}/health", headers=self._headers)
                return resp.status_code == 200 and bool(resp.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """GET from the sidecar, mapping every failure to SupplierUnavailableError."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}{path}", params=params, headers=self._headers
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as exc:
            raise SupplierUnavailableError(
                f"Home Depot sidecar returned {exc.response.status_code} for {path}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SupplierUnavailableError(f"Home Depot sidecar is unreachable: {exc}") from exc

        if not isinstance(payload, dict):
            raise SupplierUnavailableError(f"Home Depot sidecar sent an unexpected body for {path}")
        return payload

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]:
        """Search this supplier's retailer through the sidecar.

        Raises:
            SupplierUnavailableError: the sidecar is unreachable or errored, so
                the caller can fall through to the next backend.
        """
        payload = await self._get(
            "/search",
            {
                "q": query,
                "site": self.site,
                "zip": location.zip_code,
                "store_id": location.store_id,
                "limit": str(max_results),
            },
        )
        return [_to_product(p, self.name) for p in (payload.get("products") or [])[:max_results]]

    async def find_stores(
        self, near: str, *, radius_miles: int = 25, max_results: int = 5
    ) -> list[StoreResult]:
        """Find stores near a zip code, city, or address (Home Depot only).

        Raises:
            SupplierUnavailableError: the sidecar is unreachable or errored.
        """
        payload = await self._get(
            "/stores",
            {"near": near, "radius_miles": str(radius_miles), "limit": str(max_results)},
        )
        return [_to_store(s) for s in (payload.get("stores") or [])[:max_results]]


def _to_product(raw: dict[str, Any], supplier: str) -> ProductResult:
    """Map a sidecar product onto the shared :class:`ProductResult`."""
    return ProductResult(
        supplier=supplier,
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


def _to_store(raw: dict[str, Any]) -> StoreResult:
    """Map a sidecar store onto the shared :class:`StoreResult`."""
    return StoreResult(
        store_id=str(raw.get("store_id") or ""),
        name=raw.get("name") or "",
        street=raw.get("street") or "",
        city=raw.get("city") or "",
        state=raw.get("state") or "",
        zip_code=raw.get("zip_code") or "",
        phone=raw.get("phone") or "",
        distance_miles=raw.get("distance_miles"),
    )
