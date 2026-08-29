"""Brave Search web results via the Brave Search API.

https://api.search.brave.com/app/documentation/web-search/get-started

This provider does not reshape Brave's records. It pulls the result list out of
the envelope and hands them up as-is, so any field Brave sends (including ones
added after this was written) reaches the model.
"""

import asyncio
import html
import logging
import re
from typing import Any

import httpx

from backend.app.integrations.web_search.errors import SearchUnavailableError

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Brave marks query terms inside text fields with <strong> tags. Removing them
# drops markup, not information, and it is applied to every string in the record
# rather than to a named list of fields, so it needs no updating when Brave adds
# one.
_TAG_RE = re.compile(r"<[^>]+>")

# Retried once each. 429 is a rate limit and 5xx is Brave being unwell; both
# are worth exactly one more attempt inside a live message loop, where the user
# is waiting on a reply and a long retry chain reads as a hang. 4xx other than
# 429 is a fault in our request and will fail identically on retry.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5


def _clean(value: Any) -> Any:
    """Strip highlight markup and unescape entities, recursively.

    Walks the whole record rather than named fields, so nested objects (Brave's
    ``product``, ``meta_url``, ``profile``) and lists (``extra_snippets``) are
    cleaned without being enumerated here. Non-string leaves pass through
    untouched, including the numbers and booleans Brave sends.
    """
    if isinstance(value, str):
        return html.unescape(_TAG_RE.sub("", value)).strip()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


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

    async def search(self, query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
        data = await self._request(
            {
                "q": query,
                "count": str(max_results),
                # Plain web results only. Brave's news/video/location clusters
                # have their own shapes and answer a different question than the
                # one this tool asks. Product data is unaffected: Brave attaches
                # it to ordinary web results, which is where the prices live.
                "result_filter": "web",
                # No extra_snippets flag: Brave returns those passages either
                # way, measured identical byte counts with and without it, so
                # asking for them only implies a control that does not exist.
            }
        )

        results = (data.get("web") or {}).get("results") or []
        return [_clean(r) for r in results[:max_results] if isinstance(r, dict)]
