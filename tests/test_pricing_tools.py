"""Tests for supplier pricing tools (SerpApi Home Depot).

Covers:
- HomeDepotSupplier SerpApi client (HTTP, retry, parsing)
- SupplierCache TTL/eviction
- Tool function (happy path, errors, zip resolution)
- Factory gating
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.services.suppliers.cache import SupplierCache
from backend.app.services.suppliers.homedepot import HomeDepotSupplier
from backend.app.services.suppliers.protocol import Location, ProductResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_serpapi_response(products: list[dict] | None = None) -> dict:
    """Build a realistic SerpApi Home Depot search response."""
    if products is None:
        products = [
            {
                "product_id": "317061059",
                "title": "23/32 in. x 4 ft. x 8 ft. BC Sanded Pine Plywood",
                "brand": "Handprint",
                "price": 42.98,
                "previous_price": "$49.98",
                "rating": 4.5,
                "reviews": 127,
                "link": "https://www.homedepot.com/p/317061059",
                "thumbnail": "https://images.homedepot.com/317061059.jpg",
                "model_number": "166024",
                "delivery": {"free_delivery": True, "has_delivery": True},
            }
        ]
    return {"products": products}


def _make_httpx_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    resp = httpx.Response(
        status_code=status_code,
        request=httpx.Request("GET", "https://serpapi.com/search"),
        json=json_data if json_data is not None else {},
    )
    return resp


# ---------------------------------------------------------------------------
# HomeDepotSupplier tests
# ---------------------------------------------------------------------------


class TestHomeDepotSupplier:
    def test_init(self) -> None:
        s = HomeDepotSupplier(api_key="test-key")
        assert s.name == "homedepot"
        assert s.display_name == "Home Depot"

    @pytest.mark.asyncio
    async def test_search_happy_path(self) -> None:
        supplier = HomeDepotSupplier(api_key="test-key")
        mock_resp = _make_httpx_response(200, _make_serpapi_response())

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("plywood", Location(zip_code="15213"))

        assert len(results) == 1
        r = results[0]
        assert r.name == "23/32 in. x 4 ft. x 8 ft. BC Sanded Pine Plywood"
        assert r.price_dollars == 42.98
        assert r.was_price_dollars == 49.98
        assert r.in_stock is True
        assert r.supplier == "homedepot"
        assert r.brand == "Handprint"
        assert r.rating == 4.5
        assert "homedepot.com" in r.product_url

    @pytest.mark.asyncio
    async def test_search_retry_on_429_then_success(self) -> None:
        supplier = HomeDepotSupplier(api_key="test-key")
        resp_429 = _make_httpx_response(429)
        resp_200 = _make_httpx_response(200, _make_serpapi_response())

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_429, resp_200])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("backend.app.services.suppliers.homedepot.asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await supplier.search_products("plywood", Location(zip_code="15213"))

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_500_twice_raises(self) -> None:
        supplier = HomeDepotSupplier(api_key="test-key")
        resp_500 = _make_httpx_response(500)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_500, resp_500])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("backend.app.services.suppliers.homedepot.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await supplier.search_products("plywood", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_search_timeout_raises(self) -> None:
        supplier = HomeDepotSupplier(api_key="test-key")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
                return_value=mock_client,
            ),
            pytest.raises(httpx.TimeoutException),
        ):
            await supplier.search_products("plywood", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        supplier = HomeDepotSupplier(api_key="test-key")
        mock_resp = _make_httpx_response(200, {"products": []})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("nonexistent", Location(zip_code="15213"))

        assert results == []

    @pytest.mark.asyncio
    async def test_search_api_error_field(self) -> None:
        """SerpApi returns {"error": "..."} for invalid queries."""
        supplier = HomeDepotSupplier(api_key="test-key")
        mock_resp = _make_httpx_response(200, {"error": "Invalid API key"})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("plywood", Location(zip_code="15213"))

        assert results == []

    @pytest.mark.asyncio
    async def test_search_price_as_string(self) -> None:
        """SerpApi sometimes returns price as '$42.98' string."""
        supplier = HomeDepotSupplier(api_key="test-key")
        products = [{"product_id": "1", "title": "Item", "price": "$42.98"}]
        mock_resp = _make_httpx_response(200, {"products": products})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("item", Location(zip_code="15213"))

        assert results[0].price_dollars == 42.98

    @pytest.mark.asyncio
    async def test_search_missing_fields(self) -> None:
        """Products with minimal fields should parse without error."""
        supplier = HomeDepotSupplier(api_key="test-key")
        products = [{"product_id": "123", "title": "Some Item"}]
        mock_resp = _make_httpx_response(200, {"products": products})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("item", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].name == "Some Item"
        assert results[0].price_dollars is None
        assert results[0].in_stock is None

    @pytest.mark.asyncio
    async def test_search_max_results_truncation(self) -> None:
        supplier = HomeDepotSupplier(api_key="test-key")
        products = [{"product_id": str(i), "title": f"Item {i}"} for i in range(10)]
        mock_resp = _make_httpx_response(200, {"products": products})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products(
                "item", Location(zip_code="15213"), max_results=3
            )

        assert len(results) == 3


# ---------------------------------------------------------------------------
# SupplierCache tests
# ---------------------------------------------------------------------------


class TestSupplierCache:
    @pytest.mark.asyncio
    async def test_cache_miss(self) -> None:
        cache = SupplierCache()
        assert await cache.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_cache_set_and_get(self) -> None:
        cache = SupplierCache()
        await cache.set("key1", [1, 2, 3])
        assert await cache.get("key1") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self) -> None:
        cache = SupplierCache(ttl_seconds=1)
        await cache.set("key1", "value")
        assert await cache.get("key1") == "value"
        await asyncio.sleep(1.1)
        assert await cache.get("key1") is None

    @pytest.mark.asyncio
    async def test_cache_max_size_eviction(self) -> None:
        cache = SupplierCache(maxsize=2, ttl_seconds=3600)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        values = [await cache.get("a"), await cache.get("b"), await cache.get("c")]
        assert values.count(None) >= 1
        assert 3 in values

    def test_make_key_normalization(self) -> None:
        assert SupplierCache.make_key("hd", "  Plywood  ", "15213") == "hd:plywood:15213"

    def test_clear(self) -> None:
        cache = SupplierCache()
        cache._cache["test"] = "val"
        cache.clear()
        assert cache._cache.get("test") is None


# ---------------------------------------------------------------------------
# Tool function tests
# ---------------------------------------------------------------------------


class TestSupplierSearchTool:
    def _make_tool(
        self,
        results: list[ProductResult] | None = None,
        side_effect: Exception | None = None,
    ) -> tuple:
        mock_supplier = AsyncMock(spec=HomeDepotSupplier)
        mock_supplier.name = "homedepot"
        mock_supplier.display_name = "Home Depot"

        if side_effect:
            mock_supplier.search_products = AsyncMock(side_effect=side_effect)
        else:
            mock_supplier.search_products = AsyncMock(return_value=results or [])

        cache = SupplierCache()

        from backend.app.agent.tools.pricing_tools import _create_pricing_tools

        tools = _create_pricing_tools(mock_supplier, cache)
        tool_fn = tools[0].function
        return tool_fn, mock_supplier, cache

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        results = [
            ProductResult(
                supplier="homedepot",
                product_id="123",
                name="Plywood Sheet",
                brand="Handprint",
                price_dollars=42.98,
                in_stock=True,
                product_url="https://homedepot.com/p/123",
            )
        ]
        tool_fn, _, _ = self._make_tool(results=results)
        result = await tool_fn(query="plywood", zip_code="15213")

        assert not result.is_error
        assert "Plywood Sheet" in result.content
        assert "$42.98" in result.content

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self) -> None:
        results = [
            ProductResult(
                supplier="homedepot", product_id="1", name="Cached Item", price_dollars=10.0
            )
        ]
        tool_fn, mock_supplier, _cache = self._make_tool(results=results)

        await tool_fn(query="test", zip_code="15213")
        result = await tool_fn(query="test", zip_code="15213")

        assert not result.is_error
        assert "Cached Item" in result.content
        assert mock_supplier.search_products.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_zip_returns_hint(self) -> None:
        tool_fn, _, _ = self._make_tool()
        result = await tool_fn(query="test", zip_code="")

        assert result.is_error
        assert result.error_kind.value == "validation"
        assert "zip code" in result.content.lower()
        assert "USER.md" in result.hint

    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        tool_fn, _, _ = self._make_tool(side_effect=httpx.TimeoutException("timeout"))
        result = await tool_fn(query="test", zip_code="15213")

        assert result.is_error
        assert result.error_kind.value == "service"
        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_401_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "401",
            request=httpx.Request("GET", "https://serpapi.com/search"),
            response=httpx.Response(401),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)
        result = await tool_fn(query="test", zip_code="15213")

        assert result.is_error
        assert "not configured correctly" in result.content

    @pytest.mark.asyncio
    async def test_429_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://serpapi.com/search"),
            response=httpx.Response(429),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)
        result = await tool_fn(query="test", zip_code="15213")

        assert result.is_error
        assert "temporarily busy" in result.content

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        tool_fn, _, _ = self._make_tool(results=[])
        result = await tool_fn(query="nonexistent", zip_code="15213")

        assert not result.is_error
        assert "No products found" in result.content


# ---------------------------------------------------------------------------
# Factory and registration tests
# ---------------------------------------------------------------------------


class TestPricingFactory:
    def test_factory_returns_sw_only_when_no_serpapi_key(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.serpapi_api_key = ""
            result = _pricing_factory(ctx)

        assert len(result) == 1
        assert result[0].name == "supplier_search_paint"

    def test_factory_returns_both_when_key_set(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.serpapi_api_key = "test-key"
            result = _pricing_factory(ctx)

        assert len(result) == 2
        names = {t.name for t in result}
        assert "supplier_search_products" in names
        assert "supplier_search_paint" in names

    def test_auth_check_always_passes(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_auth_check

        ctx = MagicMock()
        assert _pricing_auth_check(ctx) is None

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.serpapi_api_key = ""
            assert _pricing_auth_check(ctx) is None

    def test_auth_check_returns_none_when_key_set(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_auth_check

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.serpapi_api_key = "key"
            assert _pricing_auth_check(ctx) is None
