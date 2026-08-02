"""Home Depot product search issued directly against Home Depot's own servers.

This is the keyless alternative to the SerpApi backend in ``homedepot.py``. It
talks to the same endpoints the retail site's own JavaScript uses:

* ``POST /federation-gateway/graphql?opname=psSearchModel`` for product search.
* ``GET  /StoreSearchServices/v2/storesearch`` for the store locator.

Two constraints shape this module.

**TLS impersonation is mandatory.** Both endpoints sit behind Akamai Bot
Manager, which fingerprints the TLS ClientHello. ``httpx`` and ``requests`` are
rejected outright (the store locator answers ``206`` with a generic error body;
the Canadian host simply hangs), while a Chrome-impersonating ``curl_cffi``
session is served normally. That is why this module does not use the project's
usual ``httpx`` client.

**Product search is gated and the store locator is not.** Home Depot runs its
own bot-manager layer (the ``_bman`` / ``_bman_adv`` cookies) in front of every
product route: ``/s/``, ``/b/``, ``/p/`` and the GraphQL gateway. Refused calls
get either a ``403`` carrying an "Oops!! Something went wrong" page whose script
deletes the Akamai cookies, or a ``206`` wrapping ``{"GenericError": null}``.
Both surface as :class:`HomeDepotBlockedError` so the caller can fall back to
SerpApi.

This gate is not about IP reputation. It was measured from a residential
connection, driving a real (non-headless) Chromium that solved the Akamai
challenge and rendered the homepage: the product routes still returned 403,
and PerimeterX never issued its ``_px3`` token. Retrying, warming a fresh
session, or following the error page's own cookie-reset recovery flow do not
change the outcome, so :meth:`HomeDepotDirectSupplier.search_products` treats a
block as terminal rather than retrying. ``find_stores`` sits outside all of
this and works reliably without a browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from curl_cffi.requests import AsyncSession
from pydantic import BaseModel

from backend.app.integrations.supplier_pricing.homedepot_query import SEARCH_MODEL_QUERY
from backend.app.integrations.supplier_pricing.protocol import Location, ProductResult

logger = logging.getLogger(__name__)

_ORIGIN = "https://www.homedepot.com"
_GRAPHQL_URL = f"{_ORIGIN}/federation-gateway/graphql?opname=psSearchModel"
_STORE_SEARCH_URL = f"{_ORIGIN}/StoreSearchServices/v2/storesearch"

# Home Depot serves image URLs with a literal "<SIZE>" placeholder that the
# site substitutes client-side. 400px is a reasonable thumbnail for chat.
_IMAGE_SIZE = "400"

# How long a warmed Akamai cookie jar is reused before being rebuilt.
_SESSION_TTL_SECONDS = 900.0

# Markers that mean "bot wall", not "no results".
_CHALLENGE_MARKERS = ("sec-if-cpt-container", "cpr_chlge", "_pxhd", "Access Denied")


class HomeDepotBlockedError(RuntimeError):
    """Home Depot's bot protection refused the request.

    Raised instead of returning an empty list so callers can distinguish
    "Home Depot would not talk to us" from "Home Depot has no such product"
    and fall back to another backend.
    """


class StoreResult(BaseModel):
    """A single Home Depot retail location."""

    store_id: str
    name: str
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    distance_miles: float | None = None


def _looks_blocked(status: int, body: str) -> bool:
    """Detect a bot-protection response rather than a real API answer."""
    if any(marker in body for marker in _CHALLENGE_MARKERS):
        return True
    if status in (401, 403, 429):
        return True
    # The gateway answers 206 with this envelope when PerimeterX rejects the
    # caller. A genuine GraphQL error names the field it failed on.
    return '"GenericError"' in body


def _product_url(canonical_url: str, item_id: str) -> str:
    """Build an absolute product URL from the canonical path."""
    if canonical_url:
        if canonical_url.startswith("http"):
            return canonical_url
        return f"{_ORIGIN}/{canonical_url.lstrip('/')}"
    if item_id:
        return f"{_ORIGIN}/p/{item_id}"
    return ""


def _first_image(media: dict[str, Any]) -> str:
    """Pick a thumbnail URL, resolving Home Depot's <SIZE> placeholder."""
    for image in media.get("images") or []:
        url = image.get("url") or ""
        if url:
            return url.replace("<SIZE>", _IMAGE_SIZE)
    return ""


def _stock_from_fulfillment(fulfillment: dict[str, Any] | None) -> tuple[bool | None, int | None]:
    """Reduce the fulfillment tree to (in_stock, quantity).

    Home Depot reports availability per option (pickup, delivery) and per
    location. Treat the product as in stock if any location says so, and
    surface the largest quantity seen, which is the local store's shelf count
    when a store was supplied.
    """
    if not fulfillment:
        return None, None

    saw_inventory = False
    in_stock = False
    quantity: int | None = None

    for option in fulfillment.get("fulfillmentOptions") or []:
        for service in option.get("services") or []:
            for loc in service.get("locations") or []:
                inventory = loc.get("inventory") or {}
                if not inventory:
                    continue
                saw_inventory = True
                if inventory.get("isInStock"):
                    in_stock = True
                qty = inventory.get("quantity")
                if isinstance(qty, int) and (quantity is None or qty > quantity):
                    quantity = qty

    if not saw_inventory:
        return None, None
    return in_stock, quantity


def _parse_product(raw: dict[str, Any]) -> ProductResult:
    """Map one ``searchModel.products[]`` entry onto :class:`ProductResult`."""
    identifiers = raw.get("identifiers") or {}
    pricing = raw.get("pricing") or {}
    ratings = (raw.get("reviews") or {}).get("ratingsReviews") or {}
    item_id = str(raw.get("itemId") or identifiers.get("itemId") or "")

    price = pricing.get("value")
    original = pricing.get("original")
    # Home Depot repeats the current price in `original` when nothing is on
    # sale. Only report a "was" price when it is genuinely higher.
    was_price = None
    if isinstance(original, (int, float)) and isinstance(price, (int, float)) and original > price:
        was_price = float(original)

    in_stock, quantity = _stock_from_fulfillment(raw.get("fulfillment"))
    if (raw.get("availabilityType") or {}).get("discontinued"):
        in_stock = False

    return ProductResult(
        supplier="homedepot",
        product_id=item_id,
        name=identifiers.get("productLabel") or "Unknown product",
        brand=identifiers.get("brandName") or "",
        price_dollars=float(price) if isinstance(price, (int, float)) else None,
        was_price_dollars=was_price,
        in_stock=in_stock,
        stock_quantity=quantity,
        product_url=_product_url(identifiers.get("canonicalUrl") or "", item_id),
        image_url=_first_image(raw.get("media") or {}),
        rating=ratings.get("averageRating"),
    )


def _parse_store(raw: dict[str, Any]) -> StoreResult:
    """Map one ``stores[]`` entry onto :class:`StoreResult`."""
    address = raw.get("address") or {}
    distance = raw.get("distance")
    return StoreResult(
        store_id=str(raw.get("storeId") or ""),
        name=raw.get("name") or "",
        street=address.get("street") or "",
        city=address.get("city") or "",
        state=address.get("state") or "",
        zip_code=address.get("postalCode") or "",
        phone=raw.get("phone") or "",
        distance_miles=round(float(distance), 1) if isinstance(distance, (int, float)) else None,
    )


class HomeDepotDirectSupplier:
    """Home Depot search backed by Home Depot's own endpoints. No API key.

    Instances hold a warmed cookie session and are safe to share across
    concurrent callers; access to the session is serialized by a lock.
    """

    def __init__(self, *, timeout_seconds: float = 25.0) -> None:
        self.name = "homedepot"
        self.display_name = "Home Depot"
        self._timeout = timeout_seconds
        self._session: AsyncSession | None = None
        self._session_created_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Release the underlying HTTP session."""
        async with self._lock:
            await self._discard_session()

    async def _discard_session(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                logger.debug("Error closing Home Depot session", exc_info=True)
            self._session = None
            self._session_created_at = 0.0

    async def _get_session(self) -> AsyncSession:
        """Return a warmed session, rebuilding it once the TTL has elapsed.

        The homepage request seeds the Akamai cookies (``_abck``, ``bm_*``)
        that the API endpoints expect to see on a returning visitor.
        """
        async with self._lock:
            fresh_enough = time.monotonic() - self._session_created_at < _SESSION_TTL_SECONDS
            if self._session is not None and fresh_enough:
                return self._session

            await self._discard_session()
            session = AsyncSession(impersonate="chrome")
            try:
                await session.get(f"{_ORIGIN}/", timeout=self._timeout)
            except Exception:
                # A failed warmup is not fatal; the real request may still be
                # served, and it will raise a clearer error if it is not.
                logger.debug("Home Depot session warmup failed", exc_info=True)
            self._session = session
            self._session_created_at = time.monotonic()
            return session

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]:
        """Search Home Depot for ``query``, localized to ``location``.

        Raises:
            HomeDepotBlockedError: Home Depot's bot protection refused us.
        """
        payload = {
            "operationName": "psSearchModel",
            "query": SEARCH_MODEL_QUERY,
            "variables": {
                "keyword": query,
                "itemIds": None,
                "navParam": "",
                "zipCode": location.zip_code,
                "storeId": location.store_id or "",
                "channel": "DESKTOP",
                "storefilter": "ALL",
                "pageSize": max_results,
                "startIndex": 0,
                "orderBy": {"field": "TOP_SELLERS", "order": "DESC"},
                "additionalSearchParams": {"callback": ""},
                "skipFavoriteCount": True,
                "skipInstallServices": True,
                "isBrandPricingPolicyCompliant": False,
                "loyaltyMembershipInput": None,
            },
        }
        headers = {
            "content-type": "application/json",
            "accept": "*/*",
            "origin": _ORIGIN,
            "referer": f"{_ORIGIN}/",
            # The gateway routes on these; they mirror what the site sends.
            "x-experience-name": "general-merchandise",
            "x-hd-dc": "origin",
            "x-current-url": "/",
            "x-api-cookies": "{}",
            "x-debug": "false",
            "x-thd-customer-token": "",
            "apollographql-client-name": "general-merchandise",
            "apollographql-client-version": "0.0.1",
        }

        data = await self._post_json(_GRAPHQL_URL, payload, headers)
        search_model = (data.get("data") or {}).get("searchModel") or {}
        products = search_model.get("products") or []
        return [_parse_product(p) for p in products[:max_results]]

    async def find_stores(
        self, near: str, *, radius_miles: int = 25, max_results: int = 5
    ) -> list[StoreResult]:
        """Find Home Depot stores near a zip code, city, or address.

        A non-zip ``near`` (for example ``"Denver, CO"``) makes the locator
        answer with geocoding candidates instead of stores; in that case the
        first candidate's coordinates are used for a second lookup.

        Raises:
            HomeDepotBlockedError: Home Depot's bot protection refused us.
        """
        params: dict[str, str] = {
            "address": near,
            "radius": str(radius_miles),
            "pagesize": str(max_results),
        }
        data = await self._get_json(_STORE_SEARCH_URL, params)

        if "stores" not in data and "ambiguousAddresses" in data:
            point = self._first_geocode_point(data)
            if point is None:
                return []
            data = await self._get_json(
                _STORE_SEARCH_URL,
                {
                    "latitude": str(point[0]),
                    "longitude": str(point[1]),
                    "radius": str(radius_miles),
                    "pagesize": str(max_results),
                },
            )

        return [_parse_store(s) for s in (data.get("stores") or [])[:max_results]]

    @staticmethod
    def _first_geocode_point(data: dict[str, Any]) -> tuple[float, float] | None:
        """Pull (lat, lng) out of an ambiguous-address response."""
        for ambiguous in data.get("ambiguousAddresses") or []:
            for suggestion in ambiguous.get("suggestedLocations") or []:
                coords = (suggestion.get("point") or {}).get("coordinates") or {}
                lat, lng = coords.get("lat"), coords.get("lng")
                if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                    return float(lat), float(lng)
        return None

    async def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        session = await self._get_session()
        resp = await session.get(url, params=params, timeout=self._timeout)
        return self._decode(resp.status_code, resp.text, url)

    async def _post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        session = await self._get_session()
        resp = await session.post(url, json=payload, headers=headers, timeout=self._timeout)
        return self._decode(resp.status_code, resp.text, url)

    def _decode(self, status: int, body: str, url: str) -> dict[str, Any]:
        """Validate a response and parse it, or raise HomeDepotBlockedError."""
        if _looks_blocked(status, body):
            # Force a fresh cookie jar on the next call; the current one is
            # either burned or was never trusted.
            self._session_created_at = 0.0
            logger.warning("Home Depot bot protection refused %s (status %d)", url, status)
            raise HomeDepotBlockedError(
                f"Home Depot bot protection refused the request (status {status})"
            )

        try:
            parsed = json.loads(body)
        except ValueError as exc:
            self._session_created_at = 0.0
            raise HomeDepotBlockedError(
                f"Home Depot returned a non-JSON response to {url}"
            ) from exc

        if not isinstance(parsed, dict):
            raise HomeDepotBlockedError(f"Home Depot returned an unexpected payload for {url}")
        return parsed
