"""Tests for the general web search tool.

Covers:
- BraveSearchProvider (HTTP, retry/backoff, parsing)
- SearchCache TTL/eviction
- Tool function (happy path, cache, errors, formatting)
- Provider resolution and factory/auth gating

The search API is mocked throughout; no test makes a network call.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.integrations.web_search.brave import BraveSearchProvider, _clean
from backend.app.integrations.web_search.cache import SearchCache
from backend.app.integrations.web_search.errors import SearchUnavailableError
from backend.app.integrations.web_search.protocol import SearchProvider
from backend.app.integrations.web_search.render import render_records

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_brave_response(results: list[dict] | None = None) -> dict:
    """Build a realistic Brave web-search response.

    Field shapes mirror a live response: descriptions carry ``<strong>``
    highlight markup and ``age``/``page_age`` may be ``null`` rather than
    absent.
    """
    if results is None:
        results = [
            {
                "title": "1/2 in. Copper Type L Pipe",
                "url": "https://example.com/copper-type-l",
                "description": "Type L is commonly <strong>$1.40-$2.20 per foot</strong>.",
                "age": "March 12, 2026",
                "page_age": None,
            }
        ]
    return {"web": {"results": results}}


def _make_httpx_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        request=httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search"),
        json=json_data if json_data is not None else {},
    )


def _mock_client(responses: list[httpx.Response]) -> AsyncMock:
    """An httpx.AsyncClient stub that returns *responses* in order."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@contextmanager
def _patch_client(client: AsyncMock) -> Iterator[AsyncMock]:
    """Swap the provider's httpx client for *client* for the duration."""
    with patch(
        "backend.app.integrations.web_search.brave.httpx.AsyncClient",
        return_value=client,
    ):
        yield client


# ---------------------------------------------------------------------------
# BraveSearchProvider
# ---------------------------------------------------------------------------


class TestClean:
    def test_strips_highlight_markup(self) -> None:
        assert _clean("Type L is <strong>$1.40</strong> per foot") == "Type L is $1.40 per foot"

    def test_unescapes_entities(self) -> None:
        assert _clean("3/4&quot; pipe &amp; fittings") == '3/4" pipe & fittings'

    def test_passes_non_strings_through_untouched(self) -> None:
        """Cleaning is markup removal, not coercion: numbers, booleans and the
        nulls Brave sends for absent fields keep their types."""
        assert _clean(None) is None
        assert _clean(24.99) == 24.99
        assert _clean(True) is True

    def test_cleans_nested_objects_and_lists(self) -> None:
        """Applied by walking the record, so it reaches fields nobody listed."""
        cleaned = _clean({"product": {"name": "<strong>USG</strong>"}, "extra": ["<em>a</em>"]})
        assert cleaned == {"product": {"name": "USG"}, "extra": ["a"]}


class TestBraveSearchProvider:
    def test_init(self) -> None:
        p = BraveSearchProvider(api_key="test-key")
        assert p.name == "brave"
        assert p.display_name == "Brave Search"

    def test_satisfies_the_provider_protocol(self) -> None:
        """The seam is what lets the backend be swapped, so assert on it."""
        assert isinstance(BraveSearchProvider(api_key="k"), SearchProvider)

    @pytest.mark.asyncio
    async def test_search_happy_path(self) -> None:
        provider = BraveSearchProvider(api_key="test-key")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            results = await provider.search("copper pipe price")

        assert len(results) == 1
        r = results[0]
        # Passed through with the provider's own key names, markup stripped.
        assert r["title"] == "1/2 in. Copper Type L Pipe"
        assert r["url"] == "https://example.com/copper-type-l"
        assert r["description"] == "Type L is commonly $1.40-$2.20 per foot."
        assert r["age"] == "March 12, 2026"

    @pytest.mark.asyncio
    async def test_sends_the_key_as_a_header_not_a_query_param(self) -> None:
        """A key in the URL leaks into logs and referrers."""
        provider = BraveSearchProvider(api_key="secret-key")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            await provider.search("test")

        _, kwargs = client.get.call_args
        assert kwargs["headers"]["X-Subscription-Token"] == "secret-key"
        assert "secret-key" not in str(kwargs["params"])

    @pytest.mark.asyncio
    async def test_max_results_is_requested_and_enforced(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        many = [
            {"title": f"r{i}", "url": f"https://example.com/{i}", "description": ""}
            for i in range(10)
        ]
        client = _mock_client([_make_httpx_response(200, _make_brave_response(many))])

        with _patch_client(client):
            results = await provider.search("test", max_results=3)

        _, kwargs = client.get.call_args
        assert kwargs["params"]["count"] == "3"
        # Trust the parameter, but do not depend on the provider honoring it.
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_falls_back_to_page_age_when_age_is_null(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        results_json = [
            {
                "title": "t",
                "url": "https://example.com/a",
                "description": "d",
                "age": None,
                "page_age": "2026-01-15",
            }
        ]
        client = _mock_client([_make_httpx_response(200, _make_brave_response(results_json))])

        with _patch_client(client):
            results = await provider.search("test")

        assert results[0]["page_age"] == "2026-01-15"

    @pytest.mark.asyncio
    async def test_parses_the_structured_product_price(self) -> None:
        """The price that matters is the one bound to a product name."""
        provider = BraveSearchProvider(api_key="k")
        results_json = [
            {
                "title": "USG 4.5G Plus-3 Lightweight Joint Compound Blue Lid",
                "url": "https://example.com/plus3",
                "description": "d",
                "product": {
                    "type": "Product",
                    "name": "USG 4.5G Plus-3 Lightweight Joint Compound Blue Lid",
                    "price": "24.99",
                },
            }
        ]
        client = _mock_client([_make_httpx_response(200, _make_brave_response(results_json))])

        with _patch_client(client):
            results = await provider.search("blue lid joint compound")

        # The field an allowlist would have dropped, and the reason the
        # provider seam hands records up unshaped.
        assert results[0]["product"]["price"] == "24.99"
        assert results[0]["product"]["name"] == (
            "USG 4.5G Plus-3 Lightweight Joint Compound Blue Lid"
        )

    @pytest.mark.asyncio
    async def test_result_without_a_product_has_no_price(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            results = await provider.search("test")

        assert "product" not in results[0]

    @pytest.mark.asyncio
    async def test_numeric_product_price_is_accepted(self) -> None:
        """Brave sends the price as a string, but a number must not crash it."""
        provider = BraveSearchProvider(api_key="k")
        results_json = [
            {
                "title": "t",
                "url": "https://example.com/a",
                "description": "d",
                "product": {"name": "Furring Strip", "price": 2.8},
            }
        ]
        client = _mock_client([_make_httpx_response(200, _make_brave_response(results_json))])

        with _patch_client(client):
            results = await provider.search("test")

        assert results[0]["product"]["price"] == 2.8

    @pytest.mark.asyncio
    async def test_keeps_every_record_including_ones_without_a_url(self) -> None:
        """Pass-through does not judge records. The sourcing rule in the result
        footer is what stops an unlinked figure being quoted."""
        provider = BraveSearchProvider(api_key="k")
        results_json = [
            {"title": "no link", "url": "", "description": "d"},
            {"title": "good", "url": "https://example.com/ok", "description": "d"},
        ]
        client = _mock_client([_make_httpx_response(200, _make_brave_response(results_json))])

        with _patch_client(client):
            results = await provider.search("test")

        assert [r["url"] for r in results] == ["", "https://example.com/ok"]

    @pytest.mark.asyncio
    async def test_empty_payload_yields_no_results(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(200, {})])

        with _patch_client(client):
            results = await provider.search("test")

        assert results == []

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client(
            [
                _make_httpx_response(429),
                _make_httpx_response(200, _make_brave_response()),
            ]
        )

        with (
            _patch_client(client),
            patch("backend.app.integrations.web_search.brave.asyncio.sleep", new=AsyncMock()),
        ):
            results = await provider.search("test")

        assert len(results) == 1
        assert client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_503_then_succeeds(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client(
            [
                _make_httpx_response(503),
                _make_httpx_response(200, _make_brave_response()),
            ]
        )

        with (
            _patch_client(client),
            patch("backend.app.integrations.web_search.brave.asyncio.sleep", new=AsyncMock()),
        ):
            results = await provider.search("test")

        assert len(results) == 1
        assert client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_backoff_is_exponential_and_bounded(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(429) for _ in range(3)])
        sleeper = AsyncMock()

        with (
            _patch_client(client),
            patch("backend.app.integrations.web_search.brave.asyncio.sleep", new=sleeper),
            pytest.raises(SearchUnavailableError),
        ):
            await provider.search("test")

        # Three attempts, two waits between them, each double the last.
        assert client.get.call_count == 3
        assert [c.args[0] for c in sleeper.await_args_list] == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_does_not_retry_a_client_error(self) -> None:
        """A 400 fails identically on retry, so spending attempts on it is waste."""
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(400)])

        with _patch_client(client), pytest.raises(httpx.HTTPStatusError):
            await provider.search("test")

        assert client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_auth_failure_propagates_as_status_error(self) -> None:
        provider = BraveSearchProvider(api_key="bad")
        client = _mock_client([_make_httpx_response(401)])

        with _patch_client(client), pytest.raises(httpx.HTTPStatusError) as exc:
            await provider.search("test")

        assert exc.value.response.status_code == 401


# ---------------------------------------------------------------------------
# SearchCache
# ---------------------------------------------------------------------------


class TestSearchCache:
    @pytest.mark.asyncio
    async def test_cache_miss(self) -> None:
        assert await SearchCache().get("nope") is None

    @pytest.mark.asyncio
    async def test_cache_set_and_get(self) -> None:
        cache = SearchCache()
        await cache.set("k", ["value"])
        assert await cache.get("k") == ["value"]

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self) -> None:
        cache = SearchCache(ttl_seconds=1)
        await cache.set("k", ["v"])
        assert await cache.get("k") == ["v"]
        await asyncio.sleep(1.1)
        assert await cache.get("k") is None

    @pytest.mark.asyncio
    async def test_cache_max_size_eviction(self) -> None:
        cache = SearchCache(maxsize=2)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        assert await cache.get("c") == 3
        present = [k for k in ("a", "b", "c") if await cache.get(k) is not None]
        assert len(present) == 2

    def test_make_key_normalizes_query(self) -> None:
        """A reworded-but-identical query should hit, so casing and runs of
        whitespace must not produce distinct keys."""
        assert SearchCache.make_key("brave", "  Copper   PIPE ", 5) == "brave:copper pipe:5:any"

    def test_make_key_separates_providers_and_sizes(self) -> None:
        assert SearchCache.make_key("brave", "q", 5) != SearchCache.make_key("other", "q", 5)
        assert SearchCache.make_key("brave", "q", 5) != SearchCache.make_key("brave", "q", 10)

    def test_make_key_separates_freshness(self) -> None:
        """Otherwise a search restricted to the past month is served an older
        unfiltered hit, which is the staleness the caller asked to avoid."""
        unfiltered = SearchCache.make_key("brave", "q", 5)
        assert unfiltered != SearchCache.make_key("brave", "q", 5, "pm")
        assert SearchCache.make_key("brave", "q", 5, "pm") != SearchCache.make_key(
            "brave", "q", 5, "pd"
        )

    def test_clear(self) -> None:
        cache = SearchCache()
        cache._cache["k"] = 1
        cache.clear()
        assert len(cache._cache) == 0


# ---------------------------------------------------------------------------
# web_search tool
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> dict:
    """A provider record, shaped like a live Brave web result."""
    base: dict = {
        "title": "Copper Pipe",
        "url": "https://example.com/copper",
        "description": "$1.85 per foot",
    }
    base.update(overrides)
    return base


class TestWebSearchTool:
    def _make_tool(
        self,
        results: list[dict] | None = None,
        side_effect: Exception | None = None,
    ) -> tuple:
        provider = AsyncMock(spec=BraveSearchProvider)
        provider.name = "brave"
        provider.display_name = "Brave Search"
        if side_effect:
            provider.search = AsyncMock(side_effect=side_effect)
        else:
            provider.search = AsyncMock(return_value=results or [])

        from backend.app.integrations.web_search.factory import _create_web_search_tools

        # Fresh cache per tool set so one test cannot serve another's results.
        cache = SearchCache()
        tools = _create_web_search_tools(provider, cache)
        return tools[0].function, provider, cache

    @pytest.mark.asyncio
    async def test_happy_path_includes_every_source_url(self) -> None:
        tool_fn, _, _ = self._make_tool(
            results=[
                _record(title="A", url="https://example.com/a"),
                _record(title="B", url="https://example.com/b"),
            ]
        )
        result = await tool_fn(query="copper pipe price")

        assert not result.is_error
        assert "url: https://example.com/a" in result.content
        assert "url: https://example.com/b" in result.content

    @pytest.mark.asyncio
    async def test_result_carries_the_staleness_caveat(self) -> None:
        """The framing rides with the data, not only in the system prompt: a
        cached snippet otherwise reaches the model as undated plain text."""
        tool_fn, _, _ = self._make_tool(results=[_record()])
        result = await tool_fn(query="copper pipe price")

        assert "ballpark" in result.content
        assert "never as a firm quote" in result.content

    @pytest.mark.asyncio
    async def test_footer_sends_a_broad_query_back_for_one_retry(self) -> None:
        """Measured against Brave: "how much does a bucket cost from lowes"
        returns only category pages and no price, while "Lowes 5 gallon bucket
        price" returns one cleanly. Without this the agent reports failure on
        the first phrasing and never reaches the one that works."""
        tool_fn, _, _ = self._make_tool(results=[_record()])
        content = (await tool_fn(query="how much does a bucket cost")).content

        assert "too broad" in content
        assert "search once more" in content

    @pytest.mark.asyncio
    async def test_renders_provider_fields_it_was_never_told_about(self) -> None:
        """The whole point of the pass-through: a field this code has no
        knowledge of still reaches the model."""
        tool_fn, _, _ = self._make_tool(
            results=[
                _record(
                    age="March 12, 2026",
                    product={"name": "Blue Lid Plus 3", "price": "24.99"},
                    some_future_brave_field="surfaced anyway",
                )
            ]
        )
        result = await tool_fn(query="q")

        assert "age: March 12, 2026" in result.content
        assert "product.name: Blue Lid Plus 3" in result.content
        assert "product.price: 24.99" in result.content
        assert "some_future_brave_field: surfaced anyway" in result.content

    @pytest.mark.asyncio
    async def test_cache_hit_skips_the_api(self) -> None:
        tool_fn, provider, _ = self._make_tool(results=[_record(title="Cached")])

        await tool_fn(query="copper pipe")
        result = await tool_fn(query="copper pipe")

        assert not result.is_error
        assert "Cached" in result.content
        assert provider.search.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_hit_ignores_casing_and_spacing(self) -> None:
        tool_fn, provider, _ = self._make_tool(results=[_record()])

        await tool_fn(query="copper pipe")
        await tool_fn(query="  Copper   Pipe  ")

        assert provider.search.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_query_is_a_validation_error(self) -> None:
        tool_fn, provider, _ = self._make_tool()
        result = await tool_fn(query="   ")

        assert result.is_error
        assert result.error_kind.value == "validation"
        provider.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_results_is_not_an_error(self) -> None:
        """No results is a fact about the query, not a failure to retry."""
        tool_fn, _, _ = self._make_tool(results=[])
        result = await tool_fn(query="asdkjhasd")

        assert not result.is_error
        assert "No web results" in result.content

    @pytest.mark.asyncio
    async def test_backend_unavailable_returns_a_relayable_error(self) -> None:
        tool_fn, _, _ = self._make_tool(side_effect=SearchUnavailableError("down"))
        result = await tool_fn(query="test")

        assert result.is_error
        assert result.error_kind.value == "service"
        assert "search" in result.content.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_a_relayable_error(self) -> None:
        tool_fn, _, _ = self._make_tool(side_effect=httpx.TimeoutException("timeout"))
        result = await tool_fn(query="test")

        assert result.is_error
        assert result.error_kind.value == "service"
        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_failure_hint_never_tells_the_model_to_reword(self) -> None:
        """An infrastructure failure is not the query's fault. A hint that
        suggests rewording sends the model into a retry storm (issue #1496)."""
        for exc in (SearchUnavailableError("down"), httpx.TimeoutException("t")):
            tool_fn, _, _ = self._make_tool(side_effect=exc)
            result = await tool_fn(query="test")
            assert "rewording will not help" in result.hint

    @pytest.mark.asyncio
    async def test_auth_failure_tells_the_model_not_to_retry(self) -> None:
        response = _make_httpx_response(401)
        exc = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
        tool_fn, _, _ = self._make_tool(side_effect=exc)
        result = await tool_fn(query="test")

        assert result.is_error
        assert "not configured correctly" in result.content
        assert "Do not retry" in result.hint

    @pytest.mark.asyncio
    async def test_unexpected_error_never_escapes_the_tool(self) -> None:
        """A provider bug must degrade to a tool error, never break the loop."""
        tool_fn, _, _ = self._make_tool(side_effect=RuntimeError("boom"))
        result = await tool_fn(query="test")

        assert result.is_error
        assert result.error_kind.value == "service"

    @pytest.mark.asyncio
    async def test_a_failure_is_not_cached(self) -> None:
        """Caching a failure would serve the error after the backend recovers."""
        provider = AsyncMock(spec=BraveSearchProvider)
        provider.name = "brave"
        provider.search = AsyncMock(side_effect=httpx.TimeoutException("t"))

        from backend.app.integrations.web_search.factory import _create_web_search_tools

        tool_fn = _create_web_search_tools(provider, SearchCache())[0].function
        assert (await tool_fn(query="test")).is_error

        provider.search = AsyncMock(return_value=[_record(title="Recovered")])
        result = await tool_fn(query="test")

        assert not result.is_error
        assert "Recovered" in result.content


class TestWebSearchToolDefinition:
    def _tool(self) -> Tool:
        from backend.app.integrations.web_search.factory import _create_web_search_tools

        provider = AsyncMock(spec=BraveSearchProvider)
        provider.name = "brave"
        return _create_web_search_tools(provider, SearchCache())[0]

    def test_description_stays_generic(self) -> None:
        """An agent that sees a web search tool works out what it is good for.
        Enumerating use cases spends prompt tokens to say the obvious."""
        description = self._tool().description.lower()
        assert "search the web" in description
        assert "source url" in description

    def test_usage_hint_requires_url_and_ballpark_framing(self) -> None:
        """The prompt-side guarantee: a search-sourced price is never quotable
        without its source."""
        hint = self._tool().usage_hint.lower()
        assert "source url" in hint
        assert "ballpark" in hint
        assert "never a firm quote" in hint

    def test_usage_hint_scopes_the_caveat_to_search_results(self) -> None:
        """QuickBooks totals and the user's own rate card are exact. Hedging
        those would make the assistant useless for the numbers it does know."""
        assert "connected integrations" in self._tool().usage_hint.lower()

    def test_read_only_tool_declares_no_concurrency_group(self) -> None:
        assert self._tool().concurrency_group is None


class TestRendering:
    def test_renders_every_scalar_leaf(self) -> None:
        out = render_records([{"a": "one", "n": 2, "flag": True}])
        assert "a: one" in out
        assert "n: 2" in out
        assert "flag: True" in out

    def test_dots_nested_keys_and_indexes_lists(self) -> None:
        out = render_records([{"product": {"offers": [{"priceCurrency": "USD"}]}}])
        assert "product.offers[0].priceCurrency: USD" in out

    def test_drops_only_empty_values(self) -> None:
        """Null and empty string carry nothing. Everything else is kept."""
        out = render_records([{"kept": "yes", "zero": 0, "blank": "", "missing": None}])
        assert "kept: yes" in out
        assert "zero: 0" in out
        assert "blank" not in out
        assert "missing" not in out

    def test_keeps_image_urls_like_every_other_field(self) -> None:
        """No denylist. A field the provider sent is a field the model sees."""
        out = render_records([{"thumbnail": {"src": "https://img.example.com/x.png"}}])
        assert "thumbnail.src: https://img.example.com/x.png" in out

    def test_never_truncates_a_long_value(self) -> None:
        """The failure this guards against is a price or spec silently going
        missing from a result the provider did return."""
        out = render_records([{"description": "x" * 9000}])
        assert "x" * 9000 in out
        assert "truncated" not in out

    def test_renders_every_result_it_is_given(self) -> None:
        many = [{"description": "y" * 3000, "url": f"https://example.com/{i}"} for i in range(12)]
        out = render_records(many)
        assert "[12]" in out
        assert "omitted" not in out
        for i in range(12):
            assert f"url: https://example.com/{i}" in out


class TestSearchParameters:
    """max_results and freshness are chosen per call by the agent."""

    def _tool(self, provider: AsyncMock) -> Callable[..., Awaitable[ToolResult]]:
        from backend.app.integrations.web_search.factory import _create_web_search_tools

        return _create_web_search_tools(provider, SearchCache())[0].function

    def _provider(self) -> AsyncMock:
        provider = AsyncMock(spec=BraveSearchProvider)
        provider.name = "brave"
        provider.search = AsyncMock(return_value=[_record()])
        return provider

    @pytest.mark.asyncio
    async def test_omitted_max_results_uses_the_configured_default(self) -> None:
        provider = self._provider()
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            await self._tool(provider)(query="q")

        assert provider.search.call_args.kwargs["max_results"] == 3

    @pytest.mark.asyncio
    async def test_agent_supplied_max_results_wins(self) -> None:
        provider = self._provider()
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            await self._tool(provider)(query="q", max_results=10)

        assert provider.search.call_args.kwargs["max_results"] == 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("asked", "expected"), [(50, 20), (0, 1), (-5, 1), (20, 20)])
    async def test_max_results_is_clamped_not_rejected(self, asked: int, expected: int) -> None:
        """A model asking for 50 wants "lots". Spending a turn on a validation
        error teaches it nothing, and Brave 422s above 20."""
        provider = self._provider()
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            result = await self._tool(provider)(query="q", max_results=asked)

        assert not result.is_error
        assert provider.search.call_args.kwargs["max_results"] == expected

    @pytest.mark.asyncio
    async def test_freshness_is_passed_through(self) -> None:
        provider = self._provider()
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            await self._tool(provider)(query="q", freshness="pm")

        assert provider.search.call_args.kwargs["freshness"] == "pm"

    @pytest.mark.asyncio
    async def test_freshness_defaults_to_unfiltered(self) -> None:
        """Codes and standards have old but correct answers; filtering by
        default would hide them."""
        provider = self._provider()
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            await self._tool(provider)(query="q")

        assert provider.search.call_args.kwargs["freshness"] is None

    @pytest.mark.asyncio
    async def test_different_freshness_does_not_share_a_cache_entry(self) -> None:
        """Serving a filtered query from an unfiltered hit reintroduces exactly
        the staleness the caller asked to avoid."""
        provider = self._provider()
        tool_fn = self._tool(provider)
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            await tool_fn(query="copper pipe")
            await tool_fn(query="copper pipe", freshness="pm")

        assert provider.search.call_count == 2

    @pytest.mark.asyncio
    async def test_different_result_counts_do_not_share_a_cache_entry(self) -> None:
        provider = self._provider()
        tool_fn = self._tool(provider)
        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_max_results = 3
            await tool_fn(query="copper pipe", max_results=3)
            await tool_fn(query="copper pipe", max_results=10)

        assert provider.search.call_count == 2

    def test_schema_offers_freshness_as_an_enum(self) -> None:
        """A free-text field would let the model invent a value Brave 422s on."""
        from backend.app.agent.tools.base import tool_to_function_schema
        from backend.app.integrations.web_search.factory import _create_web_search_tools

        provider = AsyncMock(spec=BraveSearchProvider)
        provider.name = "brave"
        tool = _create_web_search_tools(provider, SearchCache())[0]
        schema = tool_to_function_schema(tool)

        freshness = schema["input_schema"]["properties"]["freshness"]
        allowed = {v for branch in freshness["anyOf"] for v in branch.get("enum", [])}
        assert allowed == {"pd", "pw", "pm", "py"}

    def test_only_query_is_required(self) -> None:
        """Both new parameters are optional, so existing behavior is unchanged
        when the model omits them."""
        from backend.app.agent.tools.base import tool_to_function_schema
        from backend.app.integrations.web_search.factory import _create_web_search_tools

        provider = AsyncMock(spec=BraveSearchProvider)
        provider.name = "brave"
        tool = _create_web_search_tools(provider, SearchCache())[0]
        schema = tool_to_function_schema(tool)

        assert schema["input_schema"]["required"] == ["query"]


class TestBraveParameterMapping:
    @pytest.mark.asyncio
    async def test_count_and_freshness_reach_the_request(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            await provider.search("test", max_results=7, freshness="pm")

        params = client.get.call_args.kwargs["params"]
        assert params["count"] == "7"
        assert params["freshness"] == "pm"

    @pytest.mark.asyncio
    async def test_freshness_is_omitted_when_unset(self) -> None:
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            await provider.search("test")

        assert "freshness" not in client.get.call_args.kwargs["params"]

    @pytest.mark.asyncio
    async def test_unrecognized_freshness_is_dropped_rather_than_sent(self) -> None:
        """Brave 422s on an invalid value. An unfiltered search is the better
        fallback: the question may have an old but correct answer."""
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            await provider.search("test", freshness="last-tuesday")

        assert "freshness" not in client.get.call_args.kwargs["params"]

    @pytest.mark.asyncio
    async def test_count_is_clamped_to_the_api_ceiling(self) -> None:
        """Brave returns 422 for count above 20."""
        provider = BraveSearchProvider(api_key="k")
        client = _mock_client([_make_httpx_response(200, _make_brave_response())])

        with _patch_client(client):
            await provider.search("test", max_results=999)

        assert client.get.call_args.kwargs["params"]["count"] == "20"


# ---------------------------------------------------------------------------
# Provider resolution and factory gating
# ---------------------------------------------------------------------------


class TestProviderResolution:
    def test_returns_none_without_a_key(self) -> None:
        from backend.app.integrations.web_search.factory import _resolve_provider

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = ""
            mock_settings.web_search_provider = "brave"
            assert _resolve_provider() is None

    def test_builds_brave_by_default(self) -> None:
        from backend.app.integrations.web_search.factory import _resolve_provider

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = "key"
            mock_settings.web_search_provider = "brave"
            mock_settings.web_search_timeout_seconds = 10.0
            provider = _resolve_provider()

        assert isinstance(provider, BraveSearchProvider)
        assert provider.name == "brave"

    def test_provider_name_is_case_and_space_insensitive(self) -> None:
        from backend.app.integrations.web_search.factory import _resolve_provider

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = "key"
            mock_settings.web_search_provider = "  BRAVE "
            mock_settings.web_search_timeout_seconds = 10.0
            assert isinstance(_resolve_provider(), BraveSearchProvider)

    def test_unknown_provider_degrades_instead_of_raising(self) -> None:
        """A typo in WEB_SEARCH_PROVIDER must not crash the tool registry."""
        from backend.app.integrations.web_search.factory import _resolve_provider

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = "key"
            mock_settings.web_search_provider = "not-a-provider"
            mock_settings.web_search_timeout_seconds = 10.0
            assert _resolve_provider() is None

    def test_a_new_backend_needs_only_a_registry_entry(self) -> None:
        """The seam the swap depends on: a provider satisfying the protocol is
        selectable without touching the tool, cache, or error handling."""
        from backend.app.integrations.web_search import factory as web_search_factory

        class StubProvider:
            name = "stub"
            display_name = "Stub"

            async def search(self, query: str, *, max_results: int = 5) -> list[dict]:
                return [_record(title="from stub")]

        with (
            patch.dict(
                web_search_factory._PROVIDERS,
                {"stub": lambda key, timeout: StubProvider()},
            ),
            patch.object(web_search_factory, "settings") as mock_settings,
        ):
            mock_settings.web_search_api_key = "key"
            mock_settings.web_search_provider = "stub"
            mock_settings.web_search_timeout_seconds = 10.0
            provider = web_search_factory._resolve_provider()

        assert isinstance(provider, StubProvider)
        assert isinstance(provider, SearchProvider)


class TestWebSearchFactory:
    def test_factory_returns_no_tools_without_a_key(self) -> None:
        """No key means no tool on the schema, not a tool that always fails."""
        from backend.app.integrations.web_search.factory import _web_search_factory

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = ""
            mock_settings.web_search_provider = "brave"
            assert _web_search_factory(MagicMock()) == []

    def test_factory_returns_the_search_tool_when_configured(self) -> None:
        from backend.app.integrations.web_search.factory import _web_search_factory

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = "key"
            mock_settings.web_search_provider = "brave"
            mock_settings.web_search_timeout_seconds = 10.0
            tools = _web_search_factory(MagicMock())

        assert [t.name for t in tools] == ["web_search"]

    @pytest.mark.asyncio
    async def test_auth_check_explains_an_unconfigured_install(self) -> None:
        from backend.app.integrations.web_search.factory import _web_search_auth_check

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = ""
            mock_settings.web_search_provider = "brave"
            reason = await _web_search_auth_check(MagicMock())

        assert reason is not None
        assert "WEB_SEARCH_API_KEY" in reason
        # The user cannot fix this by connecting anything, so the model must
        # not offer them an OAuth link that does not exist.
        assert "nothing the user can connect" in reason

    @pytest.mark.asyncio
    async def test_auth_check_passes_when_configured(self) -> None:
        from backend.app.integrations.web_search.factory import _web_search_auth_check

        with patch("backend.app.integrations.web_search.factory.settings") as mock_settings:
            mock_settings.web_search_api_key = "key"
            mock_settings.web_search_provider = "brave"
            mock_settings.web_search_timeout_seconds = 10.0
            assert await _web_search_auth_check(MagicMock()) is None
