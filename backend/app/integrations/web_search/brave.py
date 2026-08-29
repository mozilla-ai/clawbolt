"""Brave Search web results via the Brave Search API.

https://api.search.brave.com/app/documentation/web-search/get-started
"""

import asyncio
import html
import logging
import re

import httpx

from backend.app.integrations.web_search.errors import SearchUnavailableError
from backend.app.integrations.web_search.protocol import SearchResult

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Brave marks query terms inside descriptions with <strong> tags. The model is
# reading plain text on a phone, so the markup is noise at best and a spurious
# formatting instruction at worst.
_TAG_RE = re.compile(r"<[^>]+>")

# Retried once each. 429 is a rate limit and 5xx is Brave being unwell; both
# are worth exactly one more attempt inside a live message loop, where the user
# is waiting on a reply and a long retry chain reads as a hang. 4xx other than
# 429 is a fault in our request and will fail identically on retry.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5


def _clean(text: str) -> str:
    """Strip Brave's highlight markup and unescape entities."""
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


class BraveSearchProvider:
    """General web search backed by the Brave Search API."""

    def __init__(self, api_key: str, *, timeout_seconds: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.name = "brave"
        self.display_name = "Brave Search"

    async def _request(self, params: dict[str, str]) -> dict:
        """GET from Brave with bounded exponential backoff.

        Raises ``SearchUnavailableError`` when every attempt is exhausted on a
        retryable status, so the caller can tell a dead backend from a query
        with no results. Other HTTP errors propagate as ``HTTPStatusError`` so
        the tool can distinguish an auth failure (bad key, operator problem)
        from a transient one.
        """
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for attempt in range(_MAX_ATTEMPTS):
                resp = await client.get(_BRAVE_ENDPOINT, params=params, headers=headers)
                if resp.status_code in _RETRY_STATUSES:
                    if attempt == _MAX_ATTEMPTS - 1:
                        # The key is in a header, so the URL is safe to omit
                        # entirely; log the status only.
                        logger.warning(
                            "Brave search failed after %d attempts (status %d)",
                            _MAX_ATTEMPTS,
                            resp.status_code,
                        )
                        raise SearchUnavailableError(
                            f"Brave returned {resp.status_code} after {_MAX_ATTEMPTS} attempts"
                        )
                    delay = _BACKOFF_BASE_SECONDS * (2**attempt)
                    logger.warning(
                        "Brave search status %d, retrying in %.1fs", resp.status_code, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
        # Unreachable: the loop either returns, raises, or continues.
        raise SearchUnavailableError("Brave search exhausted its retries")

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        data = await self._request(
            {
                "q": query,
                "count": str(max_results),
                # Plain web results only. Brave's infobox/FAQ/news blocks carry
                # their own shapes and would need their own parsing; the tool
                # promises ranked links and this keeps that promise honest.
                "result_filter": "web",
            }
        )

        raw = (data.get("web") or {}).get("results") or []
        results: list[SearchResult] = []
        for item in raw[:max_results]:
            url = item.get("url", "")
            if not url:
                # A result with no URL cannot be cited, and an uncitable price
                # is the exact thing this integration must not produce.
                continue
            results.append(
                SearchResult(
                    title=_clean(item.get("title", "")) or url,
                    url=url,
                    snippet=_clean(item.get("description", "")),
                    age=_clean(item.get("age", "") or item.get("page_age", "")),
                )
            )
        return results
