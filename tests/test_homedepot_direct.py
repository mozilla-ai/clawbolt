"""Tests for the keyless Home Depot backend that queries Home Depot directly.

Covers:
- GraphQL search-response parsing (pricing, stock, images, URLs)
- Store locator parsing, including the ambiguous-address geocode retry
- Bot-protection detection and the SerpApi fallback it triggers

All HTTP is mocked. The live endpoints are exercised by hand, not in CI: the
product search depends on outbound IP reputation and would flake.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.integrations.supplier_pricing.cache import SupplierCache
from backend.app.integrations.supplier_pricing.homedepot_direct import (
    HomeDepotBlockedError,
    HomeDepotDirectSupplier,
    StoreResult,
    _looks_blocked,
    _parse_product,
    _product_url,
    _stock_from_fulfillment,
)
from backend.app.integrations.supplier_pricing.protocol import Location, ProductResult

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_product(**overrides: object) -> dict:
    """One `searchModel.products[]` entry shaped like the live response."""
    product = {
        "itemId": "312528815",
        "identifiers": {
            "itemId": "312528815",
            "brandName": "Handprint",
            "productLabel": "20V Cordless Drill Kit",
            "canonicalUrl": "/p/Handprint-20V-Cordless-Drill-Kit/312528815",
            "modelNumber": "HP-2000",
        },
        "pricing": {"value": 99.0, "original": 129.0},
        "availabilityType": {"type": "Online", "discontinued": False},
        "media": {"images": [{"url": "https://images.thdstatic.com/a_<SIZE>.jpg"}]},
        "reviews": {"ratingsReviews": {"averageRating": 4.5, "totalReviews": 120}},
        "fulfillment": {
            "fulfillmentOptions": [
                {
                    "type": "pickup",
                    "services": [
                        {
                            "type": "bopis",
                            "locations": [
                                {
                                    "isAnchor": True,
                                    "storeName": "Midtown",
                                    "inventory": {"isInStock": True, "quantity": 14},
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }
    product.update(overrides)
    return product


def _search_response(products: list[dict] | None = None) -> str:
    body = {
        "data": {
            "searchModel": {
                "products": products if products is not None else [_make_product()],
                "searchReport": {"totalProducts": 1},
            }
        }
    }
    return json.dumps(body)


def _store_response() -> str:
    return json.dumps(
        {
            "searchReport": {"recordCount": 2},
            "stores": [
                {
                    "storeId": "0159",
                    "name": "Midtown",
                    "phone": "(555) 555-0123",
                    "address": {
                        "street": "1 Example Ave",
                        "city": "Springfield",
                        "state": "GA",
                        "postalCode": "30308",
                    },
                    "distance": 2.0720371835686495,
                }
            ],
        }
    )


def _ambiguous_response() -> str:
    return json.dumps(
        {
            "ambiguousAddresses": [
                {
                    "suggestedLocations": [
                        {
                            "name": "Springfield, GA",
                            "point": {"coordinates": {"lat": 39.73845291, "lng": -104.98485565}},
                        }
                    ]
                }
            ]
        }
    )


def _mock_response(status: int, text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


def _supplier_with_session(session: MagicMock) -> HomeDepotDirectSupplier:
    """Build a supplier whose warmed-session lookup returns `session`."""
    supplier = HomeDepotDirectSupplier()
    supplier._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    return supplier


# ---------------------------------------------------------------------------
# Pure parsing helpers
# ---------------------------------------------------------------------------


class TestParsingHelpers:
    def test_product_url_from_canonical_path(self) -> None:
        assert _product_url("/p/Thing/123", "123") == "https://www.homedepot.com/p/Thing/123"

    def test_product_url_absolute_passthrough(self) -> None:
        url = "https://www.homedepot.com/p/Thing/123"
        assert _product_url(url, "123") == url

    def test_product_url_falls_back_to_item_id(self) -> None:
        assert _product_url("", "123") == "https://www.homedepot.com/p/123"

    def test_product_url_empty_when_nothing_known(self) -> None:
        assert _product_url("", "") == ""

    def test_stock_none_when_no_inventory_reported(self) -> None:
        assert _stock_from_fulfillment(None) == (None, None)
        assert _stock_from_fulfillment({"fulfillmentOptions": []}) == (None, None)

    def test_stock_takes_largest_quantity_across_locations(self) -> None:
        fulfillment = {
            "fulfillmentOptions": [
                {
                    "services": [
                        {
                            "locations": [
                                {"inventory": {"isInStock": False, "quantity": 0}},
                                {"inventory": {"isInStock": True, "quantity": 7}},
                            ]
                        }
                    ]
                }
            ]
        }
        assert _stock_from_fulfillment(fulfillment) == (True, 7)

    def test_stock_out_when_every_location_is_out(self) -> None:
        fulfillment = {
            "fulfillmentOptions": [
                {"services": [{"locations": [{"inventory": {"isInStock": False, "quantity": 0}}]}]}
            ]
        }
        assert _stock_from_fulfillment(fulfillment) == (False, 0)


class TestBlockDetection:
    @pytest.mark.parametrize(
        "status,body",
        [
            (200, '<div id="sec-if-cpt-container">'),
            (429, '{"cpr_chlge":"true","t":"1"}'),
            (403, "Access Denied"),
            (206, '{"data":{"GenericError":null},"error":[{"message":"Generic errors"}]}'),
            (401, "{}"),
        ],
    )
    def test_flags_bot_protection(self, status: int, body: str) -> None:
        assert _looks_blocked(status, body) is True

    def test_allows_a_normal_response(self) -> None:
        assert _looks_blocked(200, _search_response()) is False

    def test_allows_an_empty_result_set(self) -> None:
        assert _looks_blocked(200, _search_response(products=[])) is False


# ---------------------------------------------------------------------------
# Product search
# ---------------------------------------------------------------------------


class TestSearchProducts:
    def test_parse_product_maps_every_field(self) -> None:
        result = _parse_product(_make_product())

        assert result.supplier == "homedepot"
        assert result.product_id == "312528815"
        assert result.name == "20V Cordless Drill Kit"
        assert result.brand == "Handprint"
        assert result.price_dollars == 99.0
        assert result.was_price_dollars == 129.0
        assert result.in_stock is True
        assert result.stock_quantity == 14
        assert result.rating == 4.5
        assert result.product_url == (
            "https://www.homedepot.com/p/Handprint-20V-Cordless-Drill-Kit/312528815"
        )
        assert result.image_url == "https://images.thdstatic.com/a_400.jpg"

    def test_no_was_price_when_not_discounted(self) -> None:
        """Home Depot repeats the current price in `original` when nothing is on sale."""
        product = _make_product(pricing={"value": 99.0, "original": 99.0})
        assert _parse_product(product).was_price_dollars is None

    def test_discontinued_product_reports_out_of_stock(self) -> None:
        product = _make_product(availabilityType={"discontinued": True})
        assert _parse_product(product).in_stock is False

    def test_minimal_product_parses_without_error(self) -> None:
        result = _parse_product({"itemId": "1"})

        assert result.product_id == "1"
        assert result.name == "Unknown product"
        assert result.price_dollars is None
        assert result.in_stock is None
        assert result.product_url == "https://www.homedepot.com/p/1"

    @pytest.mark.asyncio
    async def test_search_returns_parsed_products(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(return_value=_mock_response(200, _search_response()))
        supplier = _supplier_with_session(session)

        results = await supplier.search_products("drill", Location(zip_code="30301"))

        assert len(results) == 1
        assert results[0].name == "20V Cordless Drill Kit"

    @pytest.mark.asyncio
    async def test_search_sends_keyword_and_store_in_variables(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(return_value=_mock_response(200, _search_response()))
        supplier = _supplier_with_session(session)

        await supplier.search_products(
            "drill", Location(zip_code="30301", store_id="0121"), max_results=3
        )

        variables = session.post.call_args.kwargs["json"]["variables"]
        assert variables["keyword"] == "drill"
        assert variables["zipCode"] == "30301"
        assert variables["storeId"] == "0121"
        assert variables["pageSize"] == 3

    @pytest.mark.asyncio
    async def test_search_truncates_to_max_results(self) -> None:
        session = MagicMock()
        products = [_make_product(itemId=str(i)) for i in range(10)]
        session.post = AsyncMock(return_value=_mock_response(200, _search_response(products)))
        supplier = _supplier_with_session(session)

        results = await supplier.search_products("drill", Location(zip_code="30301"), max_results=2)

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(return_value=_mock_response(200, _search_response(products=[])))
        supplier = _supplier_with_session(session)

        assert await supplier.search_products("nothing", Location(zip_code="30301")) == []

    @pytest.mark.asyncio
    async def test_search_raises_when_bot_walled(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(return_value=_mock_response(429, '{"cpr_chlge":"true"}'))
        supplier = _supplier_with_session(session)

        with pytest.raises(HomeDepotBlockedError):
            await supplier.search_products("drill", Location(zip_code="30301"))

    @pytest.mark.asyncio
    async def test_block_forces_a_fresh_session_next_call(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(return_value=_mock_response(429, '{"cpr_chlge":"true"}'))
        supplier = _supplier_with_session(session)
        supplier._session_created_at = 1000.0

        with pytest.raises(HomeDepotBlockedError):
            await supplier.search_products("drill", Location(zip_code="30301"))

        assert supplier._session_created_at == 0.0

    @pytest.mark.asyncio
    async def test_non_json_body_raises_blocked(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(return_value=_mock_response(200, "<html>nope</html>"))
        supplier = _supplier_with_session(session)

        with pytest.raises(HomeDepotBlockedError):
            await supplier.search_products("drill", Location(zip_code="30301"))


# ---------------------------------------------------------------------------
# Store locator
# ---------------------------------------------------------------------------


class TestFindStores:
    @pytest.mark.asyncio
    async def test_find_stores_parses_results(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=_mock_response(200, _store_response()))
        supplier = _supplier_with_session(session)

        stores = await supplier.find_stores("30301")

        assert len(stores) == 1
        store = stores[0]
        assert store.store_id == "0159"
        assert store.name == "Midtown"
        assert store.street == "1 Example Ave"
        assert store.city == "Springfield"
        assert store.state == "GA"
        assert store.zip_code == "30308"
        assert store.phone == "(555) 555-0123"
        assert store.distance_miles == 2.1

    @pytest.mark.asyncio
    async def test_ambiguous_address_retries_with_coordinates(self) -> None:
        """A city name yields geocode candidates; retry with the first one's lat/lng."""
        session = MagicMock()
        session.get = AsyncMock(
            side_effect=[
                _mock_response(200, _ambiguous_response()),
                _mock_response(200, _store_response()),
            ]
        )
        supplier = _supplier_with_session(session)

        stores = await supplier.find_stores("Springfield, GA")

        assert len(stores) == 1
        assert session.get.call_count == 2
        retry_params = session.get.call_args.kwargs["params"]
        assert retry_params["latitude"] == "39.73845291"
        assert retry_params["longitude"] == "-104.98485565"
        assert "address" not in retry_params

    @pytest.mark.asyncio
    async def test_ambiguous_address_without_coordinates_returns_empty(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(
            return_value=_mock_response(200, json.dumps({"ambiguousAddresses": []}))
        )
        supplier = _supplier_with_session(session)

        assert await supplier.find_stores("nowhere") == []

    @pytest.mark.asyncio
    async def test_find_stores_passes_radius_and_limit(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=_mock_response(200, _store_response()))
        supplier = _supplier_with_session(session)

        await supplier.find_stores("30301", radius_miles=50, max_results=3)

        params = session.get.call_args.kwargs["params"]
        assert params == {"address": "30301", "radius": "50", "pagesize": "3"}

    @pytest.mark.asyncio
    async def test_find_stores_raises_when_bot_walled(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=_mock_response(200, '<div id="sec-if-cpt-container">'))
        supplier = _supplier_with_session(session)

        with pytest.raises(HomeDepotBlockedError):
            await supplier.find_stores("30301")


# ---------------------------------------------------------------------------
# Factory wiring: direct backend, SerpApi fallback, store tool
# ---------------------------------------------------------------------------


class TestFactoryWiring:
    @staticmethod
    def _tools(direct: object, fallback: object) -> dict:
        from backend.app.integrations.supplier_pricing.factory import _create_pricing_tools

        tools = _create_pricing_tools(direct, fallback, SupplierCache())  # type: ignore[arg-type]
        return {t.name: t.function for t in tools}

    @pytest.mark.asyncio
    async def test_search_prefers_the_direct_backend(self) -> None:
        product = ProductResult(supplier="homedepot", product_id="1", name="Direct Item")
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.search_products = AsyncMock(return_value=[product])
        fallback = AsyncMock()
        fallback.search_products = AsyncMock(return_value=[])

        result = await self._tools(direct, fallback)["supplier_search_products"](
            query="drill", zip_code="30301"
        )

        assert "Direct Item" in result.content
        fallback.search_products.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_falls_back_to_serpapi_when_blocked(self) -> None:
        product = ProductResult(supplier="homedepot", product_id="2", name="Fallback Item")
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.search_products = AsyncMock(side_effect=HomeDepotBlockedError("blocked"))
        fallback = AsyncMock()
        fallback.search_products = AsyncMock(return_value=[product])

        result = await self._tools(direct, fallback)["supplier_search_products"](
            query="drill", zip_code="30301"
        )

        assert not result.is_error
        assert "Fallback Item" in result.content
        fallback.search_products.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_block_without_fallback_is_a_clear_service_error(self) -> None:
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.search_products = AsyncMock(side_effect=HomeDepotBlockedError("blocked"))

        result = await self._tools(direct, None)["supplier_search_products"](
            query="drill", zip_code="30301"
        )

        assert result.is_error
        assert result.error_kind.value == "service"
        assert "blocked" in result.content.lower()
        # The agent should be steered away from a pointless retry.
        assert "supplier_find_stores" in result.hint

    @pytest.mark.asyncio
    async def test_store_id_is_forwarded_to_the_backend(self) -> None:
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.search_products = AsyncMock(return_value=[])

        await self._tools(direct, None)["supplier_search_products"](
            query="drill", zip_code="30301", store_id="0121"
        )

        location = direct.search_products.call_args.args[1]
        assert location.store_id == "0121"

    @pytest.mark.asyncio
    async def test_store_id_is_part_of_the_cache_key(self) -> None:
        """Two stores must not share a cached price."""
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.search_products = AsyncMock(return_value=[])
        search = self._tools(direct, None)["supplier_search_products"]

        await search(query="drill", zip_code="30301", store_id="0121")
        await search(query="drill", zip_code="30301", store_id="0159")

        assert direct.search_products.await_count == 2

    @pytest.mark.asyncio
    async def test_find_stores_formats_results(self) -> None:
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.find_stores = AsyncMock(
            return_value=[
                StoreResult(
                    store_id="0159",
                    name="Midtown",
                    street="1 Example Ave",
                    city="Springfield",
                    state="GA",
                    zip_code="30308",
                    phone="(555) 555-0123",
                    distance_miles=2.1,
                )
            ]
        )

        result = await self._tools(direct, None)["supplier_find_stores"](near="30301")

        assert not result.is_error
        assert "store #0159" in result.content
        assert "Midtown" in result.content
        assert "1 Example Ave, Springfield, GA 30308" in result.content
        assert "(555) 555-0123" in result.content
        assert "2.1 mi" in result.content

    @pytest.mark.asyncio
    async def test_find_stores_requires_a_location(self) -> None:
        direct = AsyncMock(spec=HomeDepotDirectSupplier)

        result = await self._tools(direct, None)["supplier_find_stores"](near="  ")

        assert result.is_error
        assert result.error_kind.value == "validation"

    @pytest.mark.asyncio
    async def test_find_stores_empty_result(self) -> None:
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.find_stores = AsyncMock(return_value=[])

        result = await self._tools(direct, None)["supplier_find_stores"](near="00000")

        assert not result.is_error
        assert "No Home Depot stores found" in result.content

    @pytest.mark.asyncio
    async def test_find_stores_caches_by_location_and_radius(self) -> None:
        direct = AsyncMock(spec=HomeDepotDirectSupplier)
        direct.find_stores = AsyncMock(return_value=[])
        find = self._tools(direct, None)["supplier_find_stores"]

        await find(near="30301", radius_miles=25)
        await find(near="30301", radius_miles=25)
        await find(near="30301", radius_miles=50)

        assert direct.find_stores.await_count == 2


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_warmup_failure_still_yields_a_session(self) -> None:
        """A failed homepage warmup must not sink the actual request."""
        session = MagicMock()
        session.get = AsyncMock(side_effect=RuntimeError("network down"))
        session.close = AsyncMock()

        with patch(
            "backend.app.integrations.supplier_pricing.homedepot_direct.AsyncSession",
            return_value=session,
        ):
            supplier = HomeDepotDirectSupplier()
            assert await supplier._get_session() is session

    @pytest.mark.asyncio
    async def test_session_is_reused_within_its_ttl(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=_mock_response(200, "{}"))
        session.close = AsyncMock()

        with patch(
            "backend.app.integrations.supplier_pricing.homedepot_direct.AsyncSession",
            return_value=session,
        ):
            supplier = HomeDepotDirectSupplier()
            first = await supplier._get_session()
            second = await supplier._get_session()

        assert first is second
        # Warmed exactly once.
        assert session.get.await_count == 1

    @pytest.mark.asyncio
    async def test_aclose_releases_the_session(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=_mock_response(200, "{}"))
        session.close = AsyncMock()

        with patch(
            "backend.app.integrations.supplier_pricing.homedepot_direct.AsyncSession",
            return_value=session,
        ):
            supplier = HomeDepotDirectSupplier()
            await supplier._get_session()
            await supplier.aclose()

        session.close.assert_awaited_once()
        assert supplier._session is None
