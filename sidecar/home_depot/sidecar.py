"""Home Depot search sidecar.

Runs a real browser and exposes Home Depot product search over a small HTTP
API so a remotely hosted Clawbolt can query it. See README.md for why this
exists and how to run it.

Home Depot's bot manager rejects plain HTTP clients on every product route, and
it also rejects a stock Playwright Chromium. What it accepts is a browser with
no automation tells: patchright (which patches the CDP ``Runtime.enable``
leak), the real Chromium sandbox (so: not running as root, no ``--no-sandbox``),
and a persistent profile that accumulates normal cookies. Requests must be
issued *by that browser*. Exporting its cookies to a plain HTTP client does not
work, which is why this is a long-lived process rather than a cookie vendor.

The browser is warmed once on startup and reused, so a search costs about a
second instead of the seven a cold start takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Header, Query
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


class BrowserBackedSearch:
    """Owns the long-lived browser and serializes searches through it."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
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

    async def stop(self) -> None:
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
        current_url = f"/b{nav_param}" if nav_param else f"/s/{encoded}"
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
    await _engine.start()
    try:
        yield
    finally:
        await _engine.stop()


app = FastAPI(title="Home Depot search sidecar", lifespan=lifespan)


async def require_token(authorization: str = Header(default="")) -> None:
    """Reject calls without the shared token, when one is configured."""
    if not AUTH_TOKEN:
        return
    if authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "bad or missing bearer token")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": _engine._page is not None}


@app.get("/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
async def search(
    q: str = Query(min_length=1, description="Product search keyword"),
    zip_code: str = Query(default="", alias="zip"),
    store_id: str = Query(default=""),
    limit: int = Query(default=5, ge=1, le=24),
) -> SearchResponse:
    return await _engine.search(q, store_id=store_id, zip_code=zip_code, page_size=limit)
