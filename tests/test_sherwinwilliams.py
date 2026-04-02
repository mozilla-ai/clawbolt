"""Tests for Sherwin-Williams supplier integration.

Covers:
- SherwinWilliamsSupplier WCS REST client (search, SKU fetch, pricing)
- _PriceHTMLParser (price extraction from GetPricingService HTML)
- Tool function (happy path, errors, caching)
- Factory registration
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.services.suppliers.cache import SupplierCache
from backend.app.services.suppliers.protocol import Location, ProductResult
from backend.app.services.suppliers.sherwinwilliams import (
    SherwinWilliamsSupplier,
    _parse_price,
    _PriceHTMLParser,
    _slugify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_response(entries: list[dict] | None = None) -> dict:
    """Build a realistic WCS REST search response."""
    if entries is None:
        entries = [
            {
                "uniqueID": "13173",
                "name": "SuperPaint Interior Acrylic Latex",
                "partNumber": "PCP_27141",
                "parentCategoryID": "21133",
                "storeID": "10051",
                "productType": "ProductBean",
                "buyable": "true",
            }
        ]
    return {"recordSetCount": str(len(entries)), "CatalogEntryView": entries}


def _make_product_response(skus: list[dict] | None = None) -> dict:
    """Build a WCS REST product detail response."""
    if skus is None:
        skus = [
            {
                "SKUUniqueID": "16329",
                "Attributes": [
                    {
                        "identifier": "ATT_calc_size__volume_or_weight_+_item_",
                        "usage": "Defining",
                        "Values": [{"identifier": "1 Gallon", "values": "1 Gallon"}],
                    },
                    {
                        "identifier": "ATT_sheen",
                        "usage": "Defining",
                        "Values": [{"identifier": "Flat", "values": "Flat"}],
                    },
                    {
                        "identifier": "ATT_calc_base_name_or_package_color_",
                        "usage": "Defining",
                        "Values": [
                            {
                                "identifier": "High Reflective White",
                                "values": "High Reflective White",
                            }
                        ],
                    },
                ],
            },
            {
                "SKUUniqueID": "16332",
                "Attributes": [
                    {
                        "identifier": "ATT_calc_size__volume_or_weight_+_item_",
                        "usage": "Defining",
                        "Values": [{"identifier": "1 Quart", "values": "1 Quart"}],
                    },
                    {
                        "identifier": "ATT_sheen",
                        "usage": "Defining",
                        "Values": [{"identifier": "Flat", "values": "Flat"}],
                    },
                ],
            },
            {
                "SKUUniqueID": "16331",
                "Attributes": [
                    {
                        "identifier": "ATT_calc_size__volume_or_weight_+_item_",
                        "usage": "Defining",
                        "Values": [{"identifier": "5 Gallon", "values": "5 Gallon"}],
                    },
                    {
                        "identifier": "ATT_sheen",
                        "usage": "Defining",
                        "Values": [{"identifier": "Flat", "values": "Flat"}],
                    },
                ],
            },
        ]
    return {
        "CatalogEntryView": [
            {
                "uniqueID": "13173",
                "name": "SuperPaint Interior Acrylic Latex",
                "SKUs": skus,
            }
        ]
    }


def _make_pricing_html(prices: dict[str, str] | None = None) -> str:
    """Build realistic GetPricingService HTML response."""
    if prices is None:
        prices = {"16329": "$80.99", "16332": "$30.99", "16331": "$399.95"}

    parts = ["<!-- BEGIN AjaxGuestPriceResponse.jsp -->"]
    for sku_id, price in prices.items():
        parts.append(f"""
            <div id="price-display-{sku_id}">
                <input type="hidden" id="ProductInfoPrice_{sku_id}" value="{price}"/>
                <div itemprop="price" id="listPrice_{sku_id}">
                    {price}
                    <span id="unitLabel_{sku_id}" class="price-block__small">/ Gallon</span>
                </div>
            </div>
        """)
    return "\n".join(parts)


def _make_httpx_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
) -> httpx.Response:
    kwargs: dict = {
        "status_code": status_code,
        "request": httpx.Request("GET", "https://www.sherwin-williams.com/test"),
    }
    if json_data is not None:
        kwargs["json"] = json_data
    else:
        kwargs["text"] = text
    return httpx.Response(**kwargs)


# ---------------------------------------------------------------------------
# _PriceHTMLParser tests
# ---------------------------------------------------------------------------


class TestPriceHTMLParser:
    def test_extracts_prices(self) -> None:
        html = _make_pricing_html({"16329": "$80.99", "16332": "$30.99"})
        parser = _PriceHTMLParser()
        parser.feed(html)
        assert parser.prices == {"16329": "$80.99", "16332": "$30.99"}

    def test_extracts_units(self) -> None:
        html = _make_pricing_html({"16329": "$80.99"})
        parser = _PriceHTMLParser()
        parser.feed(html)
        assert parser.units["16329"] == "Gallon"

    def test_skips_empty_prices(self) -> None:
        html = '<input type="hidden" id="ProductInfoPrice_999" value=""/>'
        parser = _PriceHTMLParser()
        parser.feed(html)
        assert "999" not in parser.prices

    def test_deduplicates_prices(self) -> None:
        html = """
            <input type="hidden" id="ProductInfoPrice_100" value="$42.00"/>
            <input type="hidden" id="ProductInfoPrice_100" value="$42.00"/>
        """
        parser = _PriceHTMLParser()
        parser.feed(html)
        assert parser.prices == {"100": "$42.00"}

    def test_handles_price_range(self) -> None:
        html = '<input type="hidden" id="ProductInfoPrice_200" value="$80.99 - $85.99"/>'
        parser = _PriceHTMLParser()
        parser.feed(html)
        assert parser.prices["200"] == "$80.99 - $85.99"


# ---------------------------------------------------------------------------
# _parse_price tests
# ---------------------------------------------------------------------------


class TestParsePrice:
    def test_simple_price(self) -> None:
        assert _parse_price("$80.99") == 80.99

    def test_price_range_takes_low_end(self) -> None:
        assert _parse_price("$80.99 - $85.99") == 80.99

    def test_price_with_comma(self) -> None:
        assert _parse_price("$1,299.99") == 1299.99

    def test_empty_string(self) -> None:
        assert _parse_price("") is None

    def test_invalid_string(self) -> None:
        assert _parse_price("N/A") is None


# ---------------------------------------------------------------------------
# _slugify tests
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self) -> None:
        assert _slugify("SuperPaint Interior") == "superpaint-interior"

    def test_special_chars(self) -> None:
        assert _slugify("Emerald(R) Urethane Trim Enamel") == "emeraldr-urethane-trim-enamel"

    def test_multiple_spaces(self) -> None:
        assert _slugify("  Duration   Home  ") == "duration-home"


# ---------------------------------------------------------------------------
# SherwinWilliamsSupplier tests
# ---------------------------------------------------------------------------


class TestSherwinWilliamsSupplier:
    def test_init(self) -> None:
        s = SherwinWilliamsSupplier()
        assert s.name == "sherwinwilliams"
        assert s.display_name == "Sherwin-Williams"

    @pytest.mark.asyncio
    async def test_search_happy_path(self) -> None:
        supplier = SherwinWilliamsSupplier()

        search_resp = _make_httpx_response(200, _make_search_response())
        product_resp = _make_httpx_response(200, _make_product_response())
        pricing_resp = _make_httpx_response(
            200, text=_make_pricing_html({"16329": "$80.99", "16332": "$30.99", "16331": "$399.95"})
        )

        mock_client = AsyncMock()
        # Calls: search, product detail, pricing
        mock_client.get = AsyncMock(side_effect=[search_resp, product_resp, pricing_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("superpaint", Location(zip_code="15213"))

        # Should have 3 results (one per size: 1 Gallon, 1 Quart, 5 Gallon)
        assert len(results) == 3
        assert all(r.supplier == "sherwinwilliams" for r in results)
        assert all(r.brand == "Sherwin-Williams" for r in results)

        # Find the 1 Gallon result
        gallon = [r for r in results if "1 Gallon" in r.name]
        assert len(gallon) == 1
        assert gallon[0].price_dollars == 80.99

        # Find the 1 Quart result
        quart = [r for r in results if "1 Quart" in r.name]
        assert len(quart) == 1
        assert quart[0].price_dollars == 30.99

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        supplier = SherwinWilliamsSupplier()
        search_resp = _make_httpx_response(200, {"CatalogEntryView": []})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=search_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("nonexistent", Location(zip_code="15213"))

        assert results == []

    @pytest.mark.asyncio
    async def test_search_no_skus_returns_product_level(self) -> None:
        supplier = SherwinWilliamsSupplier()
        search_resp = _make_httpx_response(200, _make_search_response())
        product_resp = _make_httpx_response(200, {"CatalogEntryView": [{"SKUs": []}]})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[search_resp, product_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("paint", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].name == "SuperPaint Interior Acrylic Latex"
        assert results[0].price_dollars is None

    @pytest.mark.asyncio
    async def test_search_retry_on_429(self) -> None:
        supplier = SherwinWilliamsSupplier()
        resp_429 = _make_httpx_response(429)
        resp_200 = _make_httpx_response(200, _make_search_response([]))

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp_429, resp_200])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "backend.app.services.suppliers.sherwinwilliams.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            results = await supplier.search_products("paint", Location(zip_code="15213"))

        assert results == []

    @pytest.mark.asyncio
    async def test_search_timeout_raises(self) -> None:
        supplier = SherwinWilliamsSupplier()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
                return_value=mock_client,
            ),
            pytest.raises(httpx.TimeoutException),
        ):
            await supplier.search_products("paint", Location(zip_code="15213"))

    @pytest.mark.asyncio
    async def test_sku_fetch_failure_returns_product_level(self) -> None:
        """If SKU fetch fails, return product-level entry without pricing."""
        supplier = SherwinWilliamsSupplier()
        search_resp = _make_httpx_response(200, _make_search_response())

        call_count = 0

        async def mock_get(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return search_resp
            raise httpx.TimeoutException("sku fetch failed")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
            return_value=mock_client,
        ):
            results = await supplier.search_products("paint", Location(zip_code="15213"))

        assert len(results) == 1
        assert results[0].price_dollars is None

    def test_extract_sku_attr(self) -> None:
        supplier = SherwinWilliamsSupplier()
        sku = {
            "Attributes": [
                {
                    "identifier": "ATT_sheen",
                    "Values": [{"values": "Satin"}],
                },
            ]
        }
        assert supplier._extract_sku_attr(sku, "ATT_sheen") == "Satin"
        assert supplier._extract_sku_attr(sku, "nonexistent") == ""

    @pytest.mark.asyncio
    async def test_fetch_prices_batching(self) -> None:
        """Ensure SKU IDs are batched into groups of 10."""
        supplier = SherwinWilliamsSupplier()
        sku_ids = [str(i) for i in range(15)]

        pricing_html_1 = _make_pricing_html({str(i): f"${10 + i}.99" for i in range(10)})
        pricing_html_2 = _make_pricing_html({str(i): f"${10 + i}.99" for i in range(10, 15)})

        resp1 = _make_httpx_response(200, text=pricing_html_1)
        resp2 = _make_httpx_response(200, text=pricing_html_2)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[resp1, resp2])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "backend.app.services.suppliers.sherwinwilliams.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "backend.app.services.suppliers.sherwinwilliams.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            prices = await supplier._fetch_prices(sku_ids)

        assert len(prices) == 15
        assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# Tool function tests
# ---------------------------------------------------------------------------


class TestPaintSearchTool:
    def _make_tool(
        self,
        results: list[ProductResult] | None = None,
        side_effect: Exception | None = None,
    ) -> tuple:
        mock_supplier = AsyncMock(spec=SherwinWilliamsSupplier)
        mock_supplier.name = "sherwinwilliams"
        mock_supplier.display_name = "Sherwin-Williams"

        if side_effect:
            mock_supplier.search_products = AsyncMock(side_effect=side_effect)
        else:
            mock_supplier.search_products = AsyncMock(return_value=results or [])

        cache = SupplierCache()

        from backend.app.agent.tools.pricing_tools import _create_paint_tools

        tools = _create_paint_tools(mock_supplier, cache)
        tool_fn = tools[0].function
        return tool_fn, mock_supplier, cache

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        results = [
            ProductResult(
                supplier="sherwinwilliams",
                product_id="16329",
                name="SuperPaint Interior Acrylic Latex (1 Gallon)",
                brand="Sherwin-Williams",
                price_dollars=80.99,
                unit="1 gallon",
                product_url="https://www.sherwin-williams.com/homeowners/products/superpaint-interior-acrylic-latex",
            )
        ]
        tool_fn, _, _ = self._make_tool(results=results)
        result = await tool_fn(query="superpaint")

        assert not result.is_error
        assert "SuperPaint" in result.content
        assert "$80.99" in result.content
        assert "Sherwin-Williams" in result.content

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self) -> None:
        results = [
            ProductResult(
                supplier="sherwinwilliams",
                product_id="1",
                name="Cached Paint",
                price_dollars=50.0,
            )
        ]
        tool_fn, mock_supplier, _cache = self._make_tool(results=results)

        await tool_fn(query="test")
        result = await tool_fn(query="test")

        assert not result.is_error
        assert "Cached Paint" in result.content
        assert mock_supplier.search_products.call_count == 1

    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        tool_fn, _, _ = self._make_tool(side_effect=httpx.TimeoutException("timeout"))
        result = await tool_fn(query="test")

        assert result.is_error
        assert result.error_kind.value == "service"
        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_429_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://www.sherwin-williams.com/test"),
            response=httpx.Response(429),
        )
        tool_fn, _, _ = self._make_tool(side_effect=exc)
        result = await tool_fn(query="test")

        assert result.is_error
        assert "temporarily busy" in result.content

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        tool_fn, _, _ = self._make_tool(results=[])
        result = await tool_fn(query="nonexistent")

        assert not result.is_error
        assert "No products found" in result.content


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestPaintFactory:
    def test_factory_always_includes_sw_tools(self) -> None:
        from backend.app.agent.tools.pricing_tools import _pricing_factory

        ctx = MagicMock()
        with patch("backend.app.agent.tools.pricing_tools.settings") as mock_settings:
            mock_settings.serpapi_api_key = ""
            result = _pricing_factory(ctx)

        # Should have SW tool even without SERPAPI_API_KEY
        assert len(result) >= 1
        sw_tools = [t for t in result if t.name == "supplier_search_paint"]
        assert len(sw_tools) == 1

    def test_factory_includes_both_when_serpapi_set(self) -> None:
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
