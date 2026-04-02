"""Tests for supplier pricing tools.

Covers all 24 code paths identified in the eng review:
- BackyardSupplier HTTP/retry/parsing (8 paths)
- SupplierCache TTL/eviction (5 paths)
- Tool function + factory (11 paths)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.services.suppliers.backyard import BackyardSupplier
from backend.app.services.suppliers.cache import SupplierCache
from backend.app.services.suppliers.protocol import Location, ProductResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_response(products: list[dict] | None = None) -> dict:
    """Build a realistic Backyard API search response."""
    if products is None:
        products = [
            {
                "product": {
                    "item_id": "317061059",
                    "title": "23/32 in. x 4 ft. x 8 ft. BC Sanded Pine Plywood",
                    "brand": "Handprint",
                    "aisle": "21",
                    "link": "https://www.homedepot.com/p/317061059",
                    "rating": 4.5,
                    "images": [{"link": "https://images.homedepot.com/317061059.jpg"}],
                    "buybox_winner": {
                        "price": 42.98,
                        "was_price": 49.98,
                        "fulfillment": {
                            "pickup_info": {
                                "in_stock": True,
                                "stock_level": 12,
                            }
                        },
                    },
                }
            }
        ]
    return {"search_results": products}


def _make_httpx_response(
    status_code: int = 200,
    json_data: dict | None = None,
) -> httpx.Response:
    """Create a mock httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        request=httpx.Request("GET", "https://api.backyardapi.com/request"),
        json=json_data if json_data is not None else {},
    )
    return resp


# ---------------------------------------------------------------------------
# BackyardSupplier tests
# ---------------------------------------------------------------------------


class TestBackyardSupplier:
    def test_init_valid_engine(self) -> None:
        s = BackyardSupplier("key", engine="homedepot")
        assert s.name == "backyard_homedepot"
        assert s.display_name == "Home Depot"

        s2 = BackyardSupplier("key", engine="lowes")
        assert s2.name == "backyard_lowes"
        assert s2.display_name == "Lowe's"

    def test_init_invalid_engine(self) -> None:
        with pytest.raises(ValueError, match="engine must be one of"):
            BackyardSupplier("key", engine="walmart")

    @pytest.mark.asyncio
    async def test_search_happy_path(self) -> None:
        supplier = BackyardSupplier("test-key", engine="homedepot")
        mock_resp = _make_httpx_response(200, _make_search_response())

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.backyard.httpx.AsyncClient", return_value=mock_client
        ):
            results = await supplier.search_products("plywood", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].name == "23/32 in. x 4 ft. x 8 ft. BC Sanded Pine Plywood"
        assert results[0].price_dollars == 42.98
        assert results[0].was_price_dollars == 49.98
        assert results[0].in_stock is True
        assert results[0].stock_quantity == 12
        assert results[0].supplier == "homedepot"

    @pytest.mark.asyncio
    async def test_search_retry_on_429_then_success(self) -> None:
        supplier = BackyardSupplier("test-key", engine="homedepot")
        resp_429 = _make_httpx_response(429)
        resp_200 = _make_httpx_response(200, _make_search_response())

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_429, resp_200])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.backyard.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("backend.app.services.suppliers.backyard.asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await supplier.search_products("plywood", Location(zip_code="15213"))

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_429_twice_raises(self) -> None:
        supplier = BackyardSupplier("test-key", engine="homedepot")
        resp_429 = _make_httpx_response(429)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_429, resp_429])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.backyard.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("backend.app.services.suppliers.backyard.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await supplier.search_products("plywood", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_search_retry_on_500_then_success(self) -> None:
        supplier = BackyardSupplier("test-key", engine="homedepot")
        resp_500 = _make_httpx_response(500)
        resp_200 = _make_httpx_response(200, _make_search_response())

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_500, resp_200])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.backyard.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("backend.app.services.suppliers.backyard.asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await supplier.search_products("plywood", Location(zip_code="15213"))

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_500_twice_raises(self) -> None:
        supplier = BackyardSupplier("test-key", engine="homedepot")
        resp_500 = _make_httpx_response(500)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_500, resp_500])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.backyard.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("backend.app.services.suppliers.backyard.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await supplier.search_products("plywood", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_search_401_raises_immediately(self) -> None:
        supplier = BackyardSupplier("bad-key", engine="homedepot")
        resp_401 = _make_httpx_response(401)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp_401)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.backyard.httpx.AsyncClient",
                return_value=mock_client,
            ),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await supplier.search_products("plywood", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_search_timeout_raises(self) -> None:
        supplier = BackyardSupplier("key", engine="homedepot")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.backyard.httpx.AsyncClient",
                return_value=mock_client,
            ),
            pytest.raises(httpx.TimeoutException),
        ):
            await supplier.search_products("plywood", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        supplier = BackyardSupplier("key", engine="homedepot")
        mock_resp = _make_httpx_response(200, {"search_results": []})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.backyard.httpx.AsyncClient", return_value=mock_client
        ):
            results = await supplier.search_products("nonexistent", Location(zip_code="15213"))

        assert results == []

    @pytest.mark.asyncio
    async def test_search_missing_fields(self) -> None:
        """Products with missing price, aisle, buybox should parse without error."""
        supplier = BackyardSupplier("key", engine="lowes")
        sparse_product = {
            "product": {
                "item_id": "123",
                "title": "Some Item",
            }
        }
        mock_resp = _make_httpx_response(200, {"search_results": [sparse_product]})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.backyard.httpx.AsyncClient", return_value=mock_client
        ):
            results = await supplier.search_products("item", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].name == "Some Item"
        assert results[0].price_dollars is None
        assert results[0].in_stock is None
        assert results[0].aisle == ""

    @pytest.mark.asyncio
    async def test_search_null_buybox(self) -> None:
        """Product with buybox_winner=None should parse."""
        supplier = BackyardSupplier("key", engine="homedepot")
        product = {
            "product": {
                "item_id": "999",
                "title": "Out of stock item",
                "buybox_winner": None,
            }
        }
        mock_resp = _make_httpx_response(200, {"search_results": [product]})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.backyard.httpx.AsyncClient", return_value=mock_client
        ):
            results = await supplier.search_products("item", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].price_dollars is None

    @pytest.mark.asyncio
    async def test_search_max_results_truncation(self) -> None:
        supplier = BackyardSupplier("key", engine="homedepot")
        products = [{"product": {"item_id": str(i), "title": f"Item {i}"}} for i in range(10)]
        mock_resp = _make_httpx_response(200, {"search_results": products})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.backyard.httpx.AsyncClient", return_value=mock_client
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
        # One of the earlier entries should have been evicted
        values = [await cache.get("a"), await cache.get("b"), await cache.get("c")]
        assert values.count(None) >= 1
        assert 3 in values  # Most recent should survive

    def test_make_key_normalization(self) -> None:
        assert SupplierCache.make_key("hd", "  Plywood  ", "15213") == "hd:plywood:15213"
        assert SupplierCache.make_key("hd", "PLYWOOD", "15213") == "hd:plywood:15213"

    def test_clear(self) -> None:
        cache = SupplierCache()
        # Synchronous set for test (bypass lock)
        cache._cache["test"] = "val"
        assert cache._cache.get("test") == "val"
        cache.clear()
        assert cache._cache.get("test") is None


# ---------------------------------------------------------------------------
# Tool function tests
# ---------------------------------------------------------------------------


class TestSupplierSearchTool:
    """Test the tool function via _create_pricing_tools."""

    def _make_tool(
        self,
        results: list[ProductResult] | None = None,
        side_effect: Exception | None = None,
    ) -> tuple:
        """Create a tool with a mock supplier."""
        mock_supplier = AsyncMock()
        mock_supplier.name = "backyard_homedepot"
        mock_supplier.display_name = "Home Depot"
        mock_supplier.engine = "homedepot"

        if side_effect:
            mock_supplier.search_products = AsyncMock(side_effect=side_effect)
        else:
            mock_supplier.search_products = AsyncMock(return_value=results or [])

        cache = SupplierCache()
        suppliers = {"homedepot": mock_supplier, "lowes": mock_supplier}

        from backend.app.agent.tools.pricing_tools import _create_pricing_tools

        tools = _create_pricing_tools(suppliers, cache)
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

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="plywood", store="homedepot", zip_code="15213")

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

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            # First call fills cache
            await tool_fn(query="test", store="homedepot", zip_code="15213")
            # Second call should hit cache
            result = await tool_fn(query="test", store="homedepot", zip_code="15213")

        assert not result.is_error
        assert "Cached Item" in result.content
        assert mock_supplier.search_products.call_count == 1  # Only called once

    @pytest.mark.asyncio
    async def test_missing_zip_returns_hint(self) -> None:
        tool_fn, _, _ = self._make_tool()

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="test", store="homedepot", zip_code="")

        assert result.is_error
        assert result.error_kind.value == "validation"
        assert "zip code" in result.content.lower()
        assert "USER.md" in result.hint

    @pytest.mark.asyncio
    async def test_invalid_store(self) -> None:
        tool_fn, _, _ = self._make_tool()

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="test", store="walmart", zip_code="15213")

        assert result.is_error
        assert result.error_kind.value == "validation"
        assert "'homedepot' or 'lowes'" in result.content

    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        tool_fn, _, _ = self._make_tool(side_effect=httpx.TimeoutException("timeout"))

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="test", store="homedepot", zip_code="15213")

        assert result.is_error
        assert result.error_kind.value == "service"
        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_401_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "401",
            request=httpx.Request("GET", "https://example.com"),
            response=httpx.Response(401),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="test", store="homedepot", zip_code="15213")

        assert result.is_error
        assert "not configured correctly" in result.content

    @pytest.mark.asyncio
    async def test_429_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://example.com"),
            response=httpx.Response(429),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="test", store="homedepot", zip_code="15213")

        assert result.is_error
        assert "temporarily busy" in result.content

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        tool_fn, _, _ = self._make_tool(results=[])

        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "key"
            result = await tool_fn(query="nonexistent", store="homedepot", zip_code="15213")

        assert not result.is_error
        assert "No products found" in result.content


# ---------------------------------------------------------------------------
# Factory and registration tests
# ---------------------------------------------------------------------------


class TestPricingFactory:
    def test_factory_returns_empty_when_no_key(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = ""
            result = _pricing_factory(ctx)

        assert result == []

    def test_factory_returns_tools_when_key_set(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = "test-key"
            result = _pricing_factory(ctx)

        assert len(result) == 1
        assert result[0].name == "supplier_search_products"

    def test_auth_check_returns_none_always(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_auth_check

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.backyard_api_key = ""
            assert _pricing_auth_check(ctx) is None

            mock_settings.backyard_api_key = "key"
            assert _pricing_auth_check(ctx) is None
