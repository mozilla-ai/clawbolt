"""Tests for supplier pricing tools.

Covers:
- HomeDepotSupplier GraphQL client (HTTP, retry, parsing)
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


def _make_graphql_response(products: list[dict] | None = None) -> dict:
    """Build a realistic Home Depot GraphQL search response."""
    if products is None:
        products = [
            {
                "itemId": "317061059",
                "identifiers": {
                    "brandName": "Handprint",
                    "modelNumber": "166024",
                    "productLabel": "23/32 in. x 4 ft. x 8 ft. BC Sanded Pine Plywood",
                    "canonicalUrl": "/p/Handprint-23-32-BC-Sanded-Pine-Plywood/317061059",
                },
                "pricing": {"value": 42.98, "original": 49.98},
                "ratingsReviews": {"averageRating": 4.5, "totalReviews": 127},
                "media": {"images": [{"url": "https://images.homedepot.com/317061059.jpg"}]},
                "fulfillment": {
                    "backordered": False,
                    "fulfillmentOptions": [
                        {
                            "type": "pickup",
                            "services": [
                                {
                                    "type": "bopis",
                                    "locations": [
                                        {
                                            "inventory": {
                                                "isInStock": True,
                                                "isLimitedQuantity": False,
                                                "quantity": 12,
                                            },
                                            "aisle": {"aisle": "21", "bay": "003"},
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            }
        ]
    return {"data": {"searchModel": {"products": products}}}


def _make_httpx_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    resp = httpx.Response(
        status_code=status_code,
        request=httpx.Request("POST", "https://apionline.homedepot.com/federation-gateway/graphql"),
        json=json_data if json_data is not None else {},
    )
    return resp


# ---------------------------------------------------------------------------
# HomeDepotSupplier tests
# ---------------------------------------------------------------------------


class TestHomeDepotSupplier:
    def test_init(self) -> None:
        s = HomeDepotSupplier()
        assert s.name == "homedepot"
        assert s.display_name == "Home Depot"
        assert s.store_id == ""

    def test_init_with_store_id(self) -> None:
        s = HomeDepotSupplier(store_id="1710")
        assert s.store_id == "1710"

    @pytest.mark.asyncio
    async def test_search_happy_path(self) -> None:
        supplier = HomeDepotSupplier()
        mock_resp = _make_httpx_response(200, _make_graphql_response())

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
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
        assert r.stock_quantity == 12
        assert r.aisle == "21, Bay 003"
        assert r.supplier == "homedepot"
        assert r.brand == "Handprint"
        assert r.rating == 4.5
        assert "homedepot.com" in r.product_url

    @pytest.mark.asyncio
    async def test_search_retry_on_500_then_success(self) -> None:
        supplier = HomeDepotSupplier()
        resp_500 = _make_httpx_response(500)
        resp_200 = _make_httpx_response(200, _make_graphql_response())

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[resp_500, resp_200])
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
        supplier = HomeDepotSupplier()
        resp_500 = _make_httpx_response(500)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[resp_500, resp_500])
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
        supplier = HomeDepotSupplier()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
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
        supplier = HomeDepotSupplier()
        mock_resp = _make_httpx_response(200, {"data": {"searchModel": {"products": []}}})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("nonexistent", Location(zip_code="15213"))

        assert results == []

    @pytest.mark.asyncio
    async def test_search_missing_fields(self) -> None:
        """Products with missing pricing, fulfillment should parse without error."""
        supplier = HomeDepotSupplier()
        sparse_product = {"itemId": "123", "identifiers": {"productLabel": "Some Item"}}
        mock_resp = _make_httpx_response(
            200, {"data": {"searchModel": {"products": [sparse_product]}}}
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
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
        assert results[0].aisle == ""

    @pytest.mark.asyncio
    async def test_search_null_pricing(self) -> None:
        supplier = HomeDepotSupplier()
        product = {
            "itemId": "999",
            "identifiers": {"productLabel": "Out of stock item"},
            "pricing": None,
        }
        mock_resp = _make_httpx_response(200, {"data": {"searchModel": {"products": [product]}}})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.homedepot.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("item", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].price_dollars is None

    @pytest.mark.asyncio
    async def test_search_max_results_truncation(self) -> None:
        supplier = HomeDepotSupplier()
        products = [
            {"itemId": str(i), "identifiers": {"productLabel": f"Item {i}"}} for i in range(10)
        ]
        mock_resp = _make_httpx_response(200, {"data": {"searchModel": {"products": products}}})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
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
        assert cache._cache.get("test") == "val"
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
        mock_supplier.store_id = ""

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
                stock_quantity=12,
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
    async def test_429_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("POST", "https://example.com"),
            response=httpx.Response(429),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)
        result = await tool_fn(query="test", zip_code="15213")

        assert result.is_error
        assert "temporarily busy" in result.content

    @pytest.mark.asyncio
    async def test_500_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "https://example.com"),
            response=httpx.Response(500),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)
        result = await tool_fn(query="test", zip_code="15213")

        assert result.is_error
        assert "Couldn't reach" in result.content

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
    def test_factory_returns_empty_when_disabled(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.homedepot_pricing_enabled = False
            result = _pricing_factory(ctx)

        assert result == []

    def test_factory_returns_tools_when_enabled(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.homedepot_pricing_enabled = True
            mock_settings.homedepot_store_id = ""
            result = _pricing_factory(ctx)

        assert len(result) == 1
        assert result[0].name == "supplier_search_products"

    def test_auth_check_returns_none_always(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_auth_check

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.homedepot_pricing_enabled = False
            assert _pricing_auth_check(ctx) is None

            mock_settings.homedepot_pricing_enabled = True
            assert _pricing_auth_check(ctx) is None
