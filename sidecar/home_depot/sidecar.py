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
import time
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import lowes
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
# Keeping retailer pages open lets their JavaScript keep allocating even when no
# one is searching. Close Chromium after an idle period and warm a fresh context
# only when the next request arrives. Set to 0 to keep the browser open.
IDLE_SECONDS = float(os.environ.get("HD_IDLE_SECONDS", "3600"))

# How long one request may spend driving a retailer's page once it holds that
# retailer's lock. This has to stay under the client's own timeout (35s, see
# backend/app/integrations/supplier_pricing/sidecar_client.py). A request that
# outlives its caller is worse than useless: it holds the lock with nobody left
# to receive the answer, and everything queued behind it spends its own budget
# waiting for a reply that will be thrown away. Eight failed lookups in one
# conversation traced back to exactly that (issue #1496).
REQUEST_BUDGET_SECONDS = float(os.environ.get("HD_REQUEST_BUDGET_SECONDS", "25"))

# The in-page fetch aborts itself at the remaining budget, which is the clean
# path: it returns a structured error and leaves the page healthy. This grace is
# for the case where the page never runs the script at all, a wedged renderer
# being the obvious one, so `evaluate` itself has to be cut loose. Kept small
# enough that budget plus grace still lands under the client's 35s.
_EVALUATE_GRACE_SECONDS = 5.0

# Lowe's results are server-rendered into the page, so the wait is a poll for the
# payload rather than a fixed settle. Roughly 6s of headroom in total.
LOWES_STATE_POLL_MS = 400
LOWES_STATE_POLL_ATTEMPTS = 15

# Home Depot maps some keywords to a category browse page instead of a keyword
# result set. The redirect path carries an "N-<token>" that has to be replayed
# as a bare navParam; passing the surrounding path returns nothing.
_NAV_RE = re.compile(r"/(N-[A-Za-z0-9]+)")

# Executed inside the page so the request carries the browser's own TLS
# fingerprint, cookies, and bot-manager state.
#
# Both scripts take a timeout in milliseconds as their last argument and hand it
# to AbortSignal.timeout. Without it a retailer that accepts the connection and
# then goes quiet, which is what a throttle or a bot-detection blackhole looks
# like from here, leaves the fetch pending forever. `page.evaluate` has no
# timeout of its own, so that pending promise used to park the whole request
# while it held the retailer's lock. Errors come back in the return value rather
# than as a thrown exception so the caller can tell an abort from a bad status
# without matching on message text.
_STORE_JS = """
async ([query, timeoutMs]) => {
  try {
    const r = await fetch("/StoreSearchServices/v2/storesearch?" + query,
                          {credentials: "include", signal: AbortSignal.timeout(timeoutMs)});
    return {status: r.status, body: await r.text()};
  } catch (e) {
    return {status: 0, body: "", error: String((e && e.name) || e)};
  }
}
"""

_FETCH_JS = """
async ([query, keyword, navParam, currentUrl, storeId, zipCode, pageSize, timeoutMs]) => {
 try {
  const r = await fetch("/federation-gateway/graphql?opname=searchModel", {
    method: "POST",
    credentials: "include",
    signal: AbortSignal.timeout(timeoutMs),
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
 } catch (e) {
  return {status: 0, body: "", error: String((e && e.name) || e)};
 }
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
        # One page per site, each parked on its own origin. Home Depot's search
        # is an in-page fetch, so the page has to already be on homedepot.com;
        # sharing a single page would mean re-warming on every site switch.
        self._site_pages: dict[str, Any] = {}
        # Consecutive Lowe's failures. A warmed page can go stale later (the edge
        # starts denying, or stops embedding the payload), and keeping it would
        # 502 every subsequent search until the process restarts. Discard after
        # two in a row rather than one, so a single transient hiccup does not cost
        # a ~20s re-warm.
        self._lowes_failures = 0
        # A lock per site, not one shared lock. Requests to one retailer must
        # serialize against each other because they drive that retailer's single
        # page, but they have no reason to block the other retailer. Sharing one
        # lock made the Lowe's pre-warm (a homepage load, a click, and two settle
        # waits, ~17s) hold off every Home Depot request right after startup,
        # close to the client's 20s timeout.
        self._locks: dict[str, asyncio.Lock] = {}
        # Lifecycle changes must wait for all active searches, but searches for
        # different retailers must remain concurrent. This lock protects only
        # the active count and context replacement, never a whole search.
        self._lifecycle_lock = asyncio.Lock()
        self._active_requests = 0
        self._last_used = time.monotonic()
        self._idle_changed = asyncio.Event()
        self.state = "starting"
        """One of starting, ready, idle, failed. Reported by /health."""

        self.error: str | None = None
        """Why startup failed, surfaced over HTTP so a broken deploy is diagnosable."""

        self._task: asyncio.Task[None] | None = None
        self._prewarm: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None

    def _lock_for(self, site: str) -> asyncio.Lock:
        """Return the per-site lock, creating it on first use.

        Safe to create lazily: the event loop is single-threaded, so no two
        coroutines can interleave between the lookup and the insert.
        """
        lock = self._locks.get(site)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[site] = lock
        return lock

    @contextlib.asynccontextmanager
    async def _timed_lock(self, site: str, what: str) -> AsyncIterator[float]:
        """Hold a retailer's lock for one request, yielding its work deadline.

        The two durations logged here answer different questions. Time queued
        says this retailer's single page is the bottleneck and requests are
        stacking up behind each other; time working says the retailer itself is
        slow. Neither was recorded before, so a search that took thirty seconds
        and one that took two looked identical in the logs and there was no way
        to tell a hang from a queue (issue #1496).
        """
        queued = time.monotonic()
        async with self._lock_for(site):
            started = time.monotonic()
            try:
                yield started + REQUEST_BUDGET_SECONDS
            finally:
                logger.info(
                    "%s: %.1fs queued, %.1fs working",
                    what,
                    started - queued,
                    time.monotonic() - started,
                )

    async def _evaluate(
        self, page: Any, script: str, args: list[Any], *, what: str, deadline: float
    ) -> dict[str, Any]:
        """Run one in-page fetch, bounded by the request's remaining budget.

        Two layers, because they fail differently. The script aborts its own
        fetch at the remaining budget and returns a structured error, which
        leaves the page healthy and reusable. `asyncio.wait_for` is the backstop
        for a page that never runs the script at all; cancelling unwinds the
        `async with` blocks above, so the retailer's lock is released either way
        and the next request is not punished for this one.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Refuse rather than granting a fresh slice. A second call inside one
            # request (the category-redirect retry) must not be able to push the
            # total past the budget, or budget plus grace stops being the bound
            # this whole design rests on.
            raise HTTPException(504, f"{what} ran out of budget before it could start")
        try:
            res = await asyncio.wait_for(
                page.evaluate(script, [*args, int(remaining * 1000)]),
                timeout=remaining + _EVALUATE_GRACE_SECONDS,
            )
        except TimeoutError as exc:
            raise HTTPException(504, f"{what} did not respond within {remaining:.0f}s") from exc
        if res.get("error"):
            raise HTTPException(504, f"{what} failed in the page: {res['error']}")
        return res

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
        async with self._lifecycle_lock:
            try:
                await self._launch_browser()
            except Exception as exc:
                # Chromium failing to launch lands here. Keep the message verbatim:
                # it is the only signal a remote operator gets, and the causes are
                # unobvious (an unwritable HOME breaks the crashpad handler, for
                # instance).
                await self._close_browser()
                self.state = "failed"
                self.error = f"{type(exc).__name__}: {exc}"
                logger.exception("browser startup failed")

    async def _launch_browser(self) -> None:
        """Launch and warm a fresh persistent browser context."""
        if self._pw is None:
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
        self._site_pages["home_depot"] = self._page
        await self._warm()
        self.state = "ready"
        self.error = None
        self._last_used = time.monotonic()
        self._idle_changed.set()
        # Pre-warm Lowe's too. Its warm costs a homepage load plus a click, so
        # the first Lowe's query would otherwise pay ~19s while Home Depot
        # answers in one. Doing it after state=ready keeps Home Depot available
        # immediately, and a failure here is not fatal: the lazy path in
        # _lowes_page still runs on demand.
        self._prewarm = asyncio.create_task(self._prewarm_lowes())
        if IDLE_SECONDS > 0:
            self._idle_task = asyncio.create_task(self._idle_recycler())

    async def _close_browser(self) -> None:
        """Close the current Chromium context while retaining the Playwright driver."""
        context, self._ctx = self._ctx, None
        self._page = None
        self._site_pages.clear()
        self._lowes_failures = 0
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()

    @contextlib.asynccontextmanager
    async def _use_browser(self) -> AsyncIterator[None]:
        """Reserve the current browser, lazily recreating it after idle eviction."""
        async with self._lifecycle_lock:
            if self.state == "idle":
                self.state = "starting"
                try:
                    await self._launch_browser()
                except Exception as exc:
                    await self._close_browser()
                    self.state = "failed"
                    self.error = f"{type(exc).__name__}: {exc}"
                    logger.exception("browser restart failed")
                    raise HTTPException(503, f"browser unavailable: {self.error}") from exc
            self.require_ready()
            self._active_requests += 1
        try:
            yield
        finally:
            async with self._lifecycle_lock:
                self._active_requests -= 1
                if self._active_requests == 0:
                    self._last_used = time.monotonic()
                    self._idle_changed.set()

    async def _evict_if_idle(self) -> bool:
        """Close Chromium when no search has used it for the configured interval."""
        async with self._lifecycle_lock:
            if (
                self.state != "ready"
                or self._active_requests != 0
                or time.monotonic() - self._last_used < IDLE_SECONDS
            ):
                return False
            await self._close_browser()
            self.state = "idle"
            logger.info(
                "browser closed after %.0fs idle; it will warm on the next request", IDLE_SECONDS
            )
            return True

    async def _idle_recycler(self) -> None:
        """Wait for idle time to elapse, then release Chromium's memory."""
        while True:
            async with self._lifecycle_lock:
                if self.state != "ready":
                    return
                timeout = max(0.0, IDLE_SECONDS - (time.monotonic() - self._last_used))
                self._idle_changed.clear()
            try:
                await asyncio.wait_for(self._idle_changed.wait(), timeout=timeout)
            except TimeoutError:
                if await self._evict_if_idle():
                    return

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
        for task in (self._idle_task, self._prewarm, self._task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._close_browser()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None

    async def _warm(self) -> None:
        """Land on the homepage so the bot manager sees a normal entry point."""
        await self._page.goto(f"{ORIGIN}/", wait_until="domcontentloaded", timeout=90_000)
        await self._page.wait_for_timeout(int(WARM_SECONDS * 1000))
        logger.info("browser warmed: %s", await self._page.title())

    async def _note_lowes_failure(self) -> None:
        """Drop a Lowe's session that has failed repeatedly so the next call re-warms.

        Without this, a page that warmed successfully but later went stale pins
        every subsequent search to a 502 until the process restarts. The threshold
        is two rather than one because a re-warm costs roughly twenty seconds, and
        a lone transient failure is not worth paying that for.
        """
        self._lowes_failures += 1
        if self._lowes_failures < 2:
            return
        page = self._site_pages.pop("lowes", None)
        self._lowes_failures = 0
        if page is not None:
            logger.warning("lowes: discarding a stale session after repeated failures")
            with contextlib.suppress(Exception):
                await page.close()

    async def _prewarm_lowes(self) -> None:
        """Warm the Lowe's page in the background, tolerating failure."""
        try:
            async with self._use_browser(), self._lock_for("lowes"):
                await self._lowes_page()
        except Exception:
            logger.warning("lowes pre-warm failed; will warm on first use", exc_info=True)

    async def _lowes_page(self) -> Any:
        """Return a page warmed for Lowe's, creating and warming it on first use.

        Lowe's needs more than a homepage visit. Navigating to /search from a
        session warmed only by the homepage is refused with a 403 edge deny; one
        organic click into a category first, and the same navigation is served.
        The click is the load-bearing step, so a page that never got one is not
        cached: keeping it would pin every later search to a session the edge
        refuses, with no way back short of a restart. Discard it instead and let
        the next request try the whole warm again.
        """
        page = self._site_pages.get("lowes")
        if page is not None:
            return page

        page = await self._ctx.new_page()
        try:
            await page.goto(f"{lowes.ORIGIN}/", wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(int(WARM_SECONDS * 1000))

            clicked = False
            try:
                link = page.locator("a[href*='/pl/']").first
                if await link.count():
                    await link.click()
                    await page.wait_for_timeout(int(WARM_SECONDS * 1000))
                    clicked = True
            except Exception:
                logger.warning("lowes: organic warm click failed", exc_info=True)
            if not clicked:
                raise HTTPException(503, "Lowe's warm-up found no category link to click")

            logger.info("lowes page warmed: %s", await page.title())
        except BaseException:
            with contextlib.suppress(Exception):
                await page.close()
            raise

        self._site_pages["lowes"] = page
        return page

    async def search_lowes(self, keyword: str, *, page_size: int) -> SearchResponse:
        """Search Lowe's by reading the search page's embedded state.

        The payload is server-rendered into the HTML, so there is nothing to wait
        for once the document arrives. Poll for it rather than sleeping a fixed
        interval: a blanket wait cost about seven seconds per query for no
        benefit, against Home Depot's sub-second GraphQL call.
        """
        async with self._use_browser():
            async with self._timed_lock("lowes", f"lowes search {keyword!r}"):
                page = await self._lowes_page()
                # The warm above keeps its own generous timeout: it runs once per
                # session and a re-warm is expensive. This navigation runs on
                # every search, so it gets the per-request budget instead of the
                # old 90s, which was nearly three times the client's patience.
                await page.goto(
                    lowes.search_url(keyword),
                    wait_until="domcontentloaded",
                    timeout=int(REQUEST_BUDGET_SECONDS * 1000),
                )
                html = await page.content()
                state = lowes.extract_preloaded_state(html)
                for _ in range(LOWES_STATE_POLL_ATTEMPTS):
                    if state is not None or lowes.is_denied(html):
                        break
                    await page.wait_for_timeout(LOWES_STATE_POLL_MS)
                    html = await page.content()
                    state = lowes.extract_preloaded_state(html)

            # Prefer a payload we actually parsed over the "Access Denied" substring,
            # which is a heuristic and could appear in legitimate page content.
            if state is None:
                await self._note_lowes_failure()
                if lowes.is_denied(html):
                    raise HTTPException(502, "Lowe's refused the search")
                raise HTTPException(502, "Lowe's search page carried no result payload")

            self._lowes_failures = 0
            return SearchResponse(
                keyword=keyword,
                total_products=lowes.total_products(state),
                used_nav_param=None,
                products=[Product(**p) for p in lowes.parse_products(state, page_size)],
            )

    async def _call(
        self,
        keyword: str,
        nav_param: str | None,
        store_id: str,
        zip_code: str,
        page_size: int,
        deadline: float,
    ) -> dict[str, Any]:
        encoded = urllib.parse.quote(keyword)
        current_url = f"/b/{nav_param}" if nav_param else f"/s/{encoded}"
        res = await self._evaluate(
            self._page,
            _FETCH_JS,
            [SEARCH_MODEL_QUERY, encoded, nav_param, current_url, store_id, zip_code, page_size],
            what="Home Depot search",
            deadline=deadline,
        )
        if res["status"] != 200:
            raise HTTPException(502, f"Home Depot returned {res['status']}")
        try:
            payload = json.loads(res["body"])
        except ValueError as exc:
            raise HTTPException(502, "Home Depot returned a non-JSON body") from exc
        return (payload.get("data") or {}).get("searchModel") or {}

    async def _store_call(self, params: dict[str, str], deadline: float) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        res = await self._evaluate(
            self._page,
            _STORE_JS,
            [query],
            what="Home Depot store locator",
            deadline=deadline,
        )
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
        async with self._use_browser():
            async with self._timed_lock("home_depot", f"home_depot stores {near!r}") as deadline:
                data = await self._store_call(
                    {"address": near, "radius": str(radius_miles), "pagesize": str(limit)},
                    deadline,
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
                        },
                        deadline,
                    )

            stores = [_parse_store(s) for s in (data.get("stores") or [])[:limit]]
            return StoresResponse(near=near, geocoded=geocoded, stores=stores)

    async def search(
        self, keyword: str, *, store_id: str, zip_code: str, page_size: int
    ) -> SearchResponse:
        async with self._use_browser():
            async with self._timed_lock("home_depot", f"home_depot search {keyword!r}") as deadline:
                model = await self._call(keyword, None, store_id, zip_code, page_size, deadline)
                used_nav: str | None = None

                # Category redirect: retry once with the bare N- token. The
                # deadline is shared with the first call rather than restarted,
                # so a slow first attempt cannot buy the retry a fresh budget.
                if not model.get("products"):
                    redirect = (model.get("metadata") or {}).get("searchRedirect") or ""
                    match = _NAV_RE.search(redirect.split("?")[0])
                    if match:
                        used_nav = match.group(1)
                        logger.info("keyword %r redirected to navParam %s", keyword, used_nav)
                        model = await self._call(
                            keyword, used_nav, store_id, zip_code, page_size, deadline
                        )

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
    site: str = Query(default="home_depot", pattern="^(home_depot|lowes)$"),
    zip_code: str = Query(default="", alias="zip"),
    store_id: str = Query(default=""),
    limit: int = Query(default=5, ge=1, le=24),
) -> SearchResponse:
    """Search a retailer for `q`.

    The two sites are reached differently. Home Depot answers a GraphQL call made
    from inside the page, so `zip` and `store_id` localize the result. Lowe's has
    no reachable product API, so its results are read out of the search page's
    embedded state and localization rides on the session's own store rather than
    on parameters.
    """
    if site == "lowes":
        return await _engine.search_lowes(q, page_size=limit)
    return await _engine.search(q, store_id=store_id, zip_code=zip_code, page_size=limit)


@app.get("/stores", response_model=StoresResponse, dependencies=[Depends(require_token)])
async def stores(
    near: str = Query(min_length=1, description="Zip code, city and state, or street address"),
    radius_miles: int = Query(default=25, ge=1, le=100),
    limit: int = Query(default=5, ge=1, le=25),
) -> StoresResponse:
    """Find Home Depot stores. Lowe's store lookup is not implemented."""
    return await _engine.find_stores(near, radius_miles=radius_miles, limit=limit)
