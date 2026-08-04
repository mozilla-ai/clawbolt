"""Home Depot sidecar: product search and store lookup, via a real browser.

Runs a browser and exposes Home Depot over a small HTTP API so a remotely
hosted Clawbolt can query it. See README.md for why this exists and how to run
it.

Home Depot's bot manager rejects plain HTTP clients, and also rejects a stock
Playwright Chromium. What it accepts is a browser with no automation tells:
patchright (which patches the CDP ``Runtime.enable`` leak) and a persistent
profile that accumulates normal cookies. Requests must be issued *by that browser*.
Exporting its cookies to a plain HTTP client does not work, which is why this is
a long-lived process rather than a cookie vendor.

The store locator served a TLS-impersonating HTTP client for a while, so an
earlier version of this integration queried it directly and skipped the browser.
That stopped working: the locator now answers such clients with a ``206``
carrying ``{"GenericError": null}`` while serving the browser normally from the
same address. Store lookup therefore goes through here too.

The browser is warmed once on startup and reused, so a request costs about a
second rather than the seven a cold start takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from patchright.async_api import async_playwright
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hd-sidecar")

ORIGIN = "https://www.homedepot.com"
QUERY_PATH = Path(__file__).with_name("search_model.graphql")
SEARCH_MODEL_QUERY = QUERY_PATH.read_text()

PROFILE_DIR = os.environ.get("HD_PROFILE_DIR", str(Path.home() / ".hd-sidecar-profile"))
AUTH_TOKEN = os.environ.get("HD_SIDECAR_TOKEN", "")
WARM_SECONDS = float(os.environ.get("HD_WARM_SECONDS", "7"))

# Home Depot maps some keywords to a category browse page instead of a keyword
# result set. The redirect path carries an "N-<token>" that has to be replayed
# as a bare navParam; passing the surrounding path returns nothing.
_NAV_RE = re.compile(r"/(N-[A-Za-z0-9]+)")

# Executed inside the page so the request carries the browser's own TLS
# fingerprint, cookies, and bot-manager state.
_STORE_JS = """
async ([query]) => {
  const r = await fetch("/StoreSearchServices/v2/storesearch?" + query,
                        {credentials: "include"});
  return {status: r.status, body: await r.text()};
}
"""

_FETCH_JS = """
async ([query, keyword, navParam, currentUrl, storeId, zipCode, pageSize]) => {
  const r = await fetch("/federation-gateway/graphql?opname=searchModel", {
    method: "POST",
    credentials: "include",
    headers: {
      "content-type": "application/json",
      "accept": "*/*",
      "accept-language": "en-US",
      "x-experience-name": "search-desktop",
      "x-hd-dc": "origin",
      "x-current-url": currentUrl,
      "x-debug": "false",
      "x-thd-customer-token": "",
      "x-api-cookies": "{}",
    },
    body: JSON.stringify({
      operationName: "searchModel",
      query,
      variables: {
        storefilter: "ALL",
        channel: "DESKTOP",
        skipInstallServices: false,
        skipFavoriteCount: false,
        skipKPF: true,
        skipSpecificationGroup: false,
        skipDiscoveryZones: false,
        skipBuyitagain: true,
        adBlocker: "no ad blocker detected",
        additionalSearchParams: {sponsored: true, deliveryZip: zipCode, multiStoreIds: []},
        filter: {},
        isBrandPage: false,
        isBrandPricingPolicyCompliant: false,
        keyword: navParam ? null : keyword,
        navParam: navParam,
        orderBy: {field: "BEST_MATCH", order: "ASC"},
        pageSize: pageSize,
        startIndex: 0,
        storeId: storeId,
      },
    }),
  });
  return {status: r.status, body: await r.text()};
}
"""


class Product(BaseModel):
    item_id: str
    name: str
    brand: str = ""
    model_number: str = ""
    price_dollars: float | None = None
    was_price_dollars: float | None = None
    in_stock: bool | None = None
    stock_quantity: int | None = None
    rating: float | None = None
    review_count: int | None = None
    product_url: str = ""
    image_url: str = ""


class SearchResponse(BaseModel):
    keyword: str
    total_products: int | None = None
    used_nav_param: str | None = None
    products: list[Product]


class Store(BaseModel):
    store_id: str
    name: str
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    distance_miles: float | None = None


class StoresResponse(BaseModel):
    near: str
    geocoded: bool = False
    """True when `near` was not a zip and had to be resolved to coordinates."""

    stores: list[Store]


class BrowserBackedSearch:
    """Owns the long-lived browser and serializes searches through it."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self.state = "starting"
        """One of starting, ready, failed. Reported by /health."""

        self.error: str | None = None
        """Why startup failed, surfaced over HTTP so a broken deploy is diagnosable."""

        self._task: asyncio.Task[None] | None = None

    def start_background(self) -> None:
        """Launch the browser without blocking the caller.

        Startup takes 15-25s: Chromium has to launch and load the homepage. Doing
        that inside the ASGI lifespan means the port is not listening yet, so a
        platform healthcheck sees a dead container and a failure produces no HTTP
        response at all, only container logs. Binding first and reporting status
        on /health turns both cases into something `curl` can diagnose.
        """
        self._task = asyncio.create_task(self._start())

    async def _start(self) -> None:
        try:
            self._pw = await async_playwright().start()
            self._ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                channel="chromium",
                headless=False,
                no_viewport=True,
                locale="en-US",
                timezone_id="America/New_York",
            )
            self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            await self._warm()
            self.state = "ready"
            self.error = None
        except Exception as exc:
            # Chromium failing to launch lands here. Keep the message verbatim:
            # it is the only signal a remote operator gets, and the causes are
            # unobvious (an unwritable HOME breaks the crashpad handler, for
            # instance).
            self.state = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            logger.exception("browser startup failed")

    def require_ready(self) -> None:
        """Reject work with a useful reason while the browser is not usable."""
        if self.state == "ready":
            return
        if self.state == "failed":
            raise HTTPException(503, f"browser unavailable: {self.error}")
        raise HTTPException(503, "browser is still starting, retry shortly")

    async def alive(self) -> bool:
        """Round-trip a trivial expression through the page to prove it responds."""
        if self._page is None or self.state != "ready":
            return False
        try:
            return await self._page.evaluate("1 + 1") == 2
        except Exception:
            logger.warning("browser is no longer responding", exc_info=True)
            return False

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for closer in (self._ctx, self._pw):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await (closer.close() if closer is self._ctx else closer.stop())

    async def _warm(self) -> None:
        """Land on the homepage so the bot manager sees a normal entry point."""
        await self._page.goto(f"{ORIGIN}/", wait_until="domcontentloaded", timeout=90_000)
        await self._page.wait_for_timeout(int(WARM_SECONDS * 1000))
        logger.info("browser warmed: %s", await self._page.title())

    async def _call(
        self, keyword: str, nav_param: str | None, store_id: str, zip_code: str, page_size: int
    ) -> dict[str, Any]:
        encoded = urllib.parse.quote(keyword)
        current_url = f"/b/{nav_param}" if nav_param else f"/s/{encoded}"
        res = await self._page.evaluate(
            _FETCH_JS,
            [SEARCH_MODEL_QUERY, encoded, nav_param, current_url, store_id, zip_code, page_size],
        )
        if res["status"] != 200:
            raise HTTPException(502, f"Home Depot returned {res['status']}")
        try:
            payload = json.loads(res["body"])
        except ValueError as exc:
            raise HTTPException(502, "Home Depot returned a non-JSON body") from exc
        return (payload.get("data") or {}).get("searchModel") or {}

    async def _store_call(self, params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        res = await self._page.evaluate(_STORE_JS, [query])
        if res["status"] != 200:
            raise HTTPException(502, f"Home Depot store locator returned {res['status']}")
        try:
            payload = json.loads(res["body"])
        except ValueError as exc:
            raise HTTPException(502, "Store locator returned a non-JSON body") from exc
        if "GenericError" in res["body"] and "stores" not in payload:
            raise HTTPException(502, "Home Depot refused the store lookup")
        return payload

    async def find_stores(self, near: str, *, radius_miles: int, limit: int) -> StoresResponse:
        """Look up stores near a zip code, city, or address.

        A non-zip `near` (for example "Denver, CO") makes the locator answer with
        geocoding candidates instead of stores, so the first candidate's
        coordinates drive a second lookup.
        """
        async with self._lock:
            data = await self._store_call(
                {"address": near, "radius": str(radius_miles), "pagesize": str(limit)}
            )
            geocoded = False
            if "stores" not in data and "ambiguousAddresses" in data:
                point = _first_geocode_point(data)
                if point is None:
                    return StoresResponse(near=near, geocoded=False, stores=[])
                geocoded = True
                data = await self._store_call(
                    {
                        "latitude": str(point[0]),
                        "longitude": str(point[1]),
                        "radius": str(radius_miles),
                        "pagesize": str(limit),
                    }
                )

        stores = [_parse_store(s) for s in (data.get("stores") or [])[:limit]]
        return StoresResponse(near=near, geocoded=geocoded, stores=stores)

    async def search(
        self, keyword: str, *, store_id: str, zip_code: str, page_size: int
    ) -> SearchResponse:
        async with self._lock:
            model = await self._call(keyword, None, store_id, zip_code, page_size)
            used_nav: str | None = None

            # Category redirect: retry once with the bare N- token.
            if not model.get("products"):
                redirect = (model.get("metadata") or {}).get("searchRedirect") or ""
                match = _NAV_RE.search(redirect.split("?")[0])
                if match:
                    used_nav = match.group(1)
                    logger.info("keyword %r redirected to navParam %s", keyword, used_nav)
                    model = await self._call(keyword, used_nav, store_id, zip_code, page_size)

        report = model.get("searchReport") or {}
        products = [_parse_product(p) for p in (model.get("products") or [])[:page_size]]
        return SearchResponse(
            keyword=keyword,
            total_products=report.get("totalProducts"),
            used_nav_param=used_nav,
            products=products,
        )


def _parse_product(raw: dict[str, Any]) -> Product:
    identifiers = raw.get("identifiers") or {}
    pricing = raw.get("pricing") or {}
    ratings = (raw.get("reviews") or {}).get("ratingsReviews") or {}
    item_id = str(raw.get("itemId") or identifiers.get("itemId") or "")

    price = pricing.get("value")
    original = pricing.get("original")
    was_price = None
    if isinstance(original, (int, float)) and isinstance(price, (int, float)) and original > price:
        was_price = float(original)

    in_stock, quantity = _stock(raw.get("fulfillment"))
    if (raw.get("availabilityType") or {}).get("discontinued"):
        in_stock = False

    canonical = identifiers.get("canonicalUrl") or ""
    if canonical and not canonical.startswith("http"):
        canonical = f"{ORIGIN}/{canonical.lstrip('/')}"
    elif not canonical and item_id:
        canonical = f"{ORIGIN}/p/{item_id}"

    image = ""
    for img in (raw.get("media") or {}).get("images") or []:
        if img.get("url"):
            image = img["url"].replace("<SIZE>", "400")
            break

    return Product(
        item_id=item_id,
        name=identifiers.get("productLabel") or "Unknown product",
        brand=identifiers.get("brandName") or "",
        model_number=identifiers.get("modelNumber") or "",
        price_dollars=float(price) if isinstance(price, (int, float)) else None,
        was_price_dollars=was_price,
        in_stock=in_stock,
        stock_quantity=quantity,
        rating=ratings.get("averageRating"),
        review_count=ratings.get("totalReviews"),
        product_url=canonical,
        image_url=image,
    )


def _parse_store(raw: dict[str, Any]) -> Store:
    """Map one `stores[]` entry from the locator onto :class:`Store`."""
    address = raw.get("address") or {}
    distance = raw.get("distance")
    return Store(
        store_id=str(raw.get("storeId") or ""),
        name=raw.get("name") or "",
        street=address.get("street") or "",
        city=address.get("city") or "",
        state=address.get("state") or "",
        zip_code=address.get("postalCode") or "",
        phone=raw.get("phone") or "",
        distance_miles=round(float(distance), 1) if isinstance(distance, (int, float)) else None,
    )


def _first_geocode_point(data: dict[str, Any]) -> tuple[float, float] | None:
    """Pull (lat, lng) out of an ambiguous-address response."""
    for ambiguous in data.get("ambiguousAddresses") or []:
        for suggestion in ambiguous.get("suggestedLocations") or []:
            coords = (suggestion.get("point") or {}).get("coordinates") or {}
            lat, lng = coords.get("lat"), coords.get("lng")
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                return float(lat), float(lng)
    return None


def _stock(fulfillment: dict[str, Any] | None) -> tuple[bool | None, int | None]:
    if not fulfillment:
        return None, None
    saw = False
    in_stock = False
    quantity: int | None = None
    for option in fulfillment.get("fulfillmentOptions") or []:
        for service in option.get("services") or []:
            for loc in service.get("locations") or []:
                inv = loc.get("inventory") or {}
                if not inv:
                    continue
                saw = True
                if inv.get("isInStock"):
                    in_stock = True
                qty = inv.get("quantity")
                if isinstance(qty, int) and (quantity is None or qty > quantity):
                    quantity = qty
    return (in_stock, quantity) if saw else (None, None)


_engine = BrowserBackedSearch()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not AUTH_TOKEN:
        logger.warning(
            "HD_SIDECAR_TOKEN is unset: /search is unauthenticated. Anyone who can reach "
            "this port can drive the browser. Only acceptable when bound to loopback."
        )
    # Deliberately not awaited: see BrowserBackedSearch.start_background.
    _engine.start_background()
    try:
        yield
    finally:
        await _engine.stop()


app = FastAPI(title="Home Depot search sidecar", lifespan=lifespan)


async def require_token(authorization: str = Header(default="")) -> None:
    """Reject calls without the shared token, when one is configured."""
    if not AUTH_TOKEN:
        return
    # compare_digest, not ==, so a wrong token cannot be recovered byte by byte
    # from response timing.
    if not secrets.compare_digest(authorization, f"Bearer {AUTH_TOKEN}"):
        raise HTTPException(401, "bad or missing bearer token")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Report whether the browser can actually execute, and why not if it cannot.

    A crashed or detached browser leaves the page object in place, so presence
    is not health; this evaluates in the page to prove the other end responds.
    ``state`` and ``error`` make a failed startup diagnosable over HTTP instead
    of only in container logs.
    """
    return {"ok": await _engine.alive(), "state": _engine.state, "error": _engine.error}


@app.get("/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
async def search(
    q: str = Query(min_length=1, description="Product search keyword"),
    zip_code: str = Query(default="", alias="zip"),
    store_id: str = Query(default=""),
    limit: int = Query(default=5, ge=1, le=24),
) -> SearchResponse:
    _engine.require_ready()
    return await _engine.search(q, store_id=store_id, zip_code=zip_code, page_size=limit)


@app.get("/stores", response_model=StoresResponse, dependencies=[Depends(require_token)])
async def stores(
    near: str = Query(min_length=1, description="Zip code, city and state, or street address"),
    radius_miles: int = Query(default=25, ge=1, le=100),
    limit: int = Query(default=5, ge=1, le=25),
) -> StoresResponse:
    _engine.require_ready()
    return await _engine.find_stores(near, radius_miles=radius_miles, limit=limit)
