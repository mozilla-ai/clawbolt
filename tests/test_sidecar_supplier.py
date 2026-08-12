"""Tests for the browser-sidecar supplier client and the backend chain.

The sidecar itself drives a real browser and is exercised by hand (see
sidecar/home_depot/README.md). What is covered here is the client that calls it,
for product search and store lookup at both retailers, plus the fall-through the
factory builds: sidecar then SerpApi for Home Depot, sidecar only for Lowe's
(SerpApi has no Lowe's engine) and for store lookup.
"""

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.agent.tools.base import ToolErrorKind, ToolResult
from backend.app.integrations.supplier_pricing.cache import SupplierCache
from backend.app.integrations.supplier_pricing.errors import SupplierUnavailableError
from backend.app.integrations.supplier_pricing.factory import (
    _create_pricing_tools,
    _pricing_factory,
)
from backend.app.integrations.supplier_pricing.protocol import (
    Location,
    ProductResult,
    StoreResult,
)
from backend.app.integrations.supplier_pricing.sidecar_client import SidecarSupplier

SIDECAR_URL = "http://sidecar.test:8899"


def _payload(products: list[dict] | None = None) -> dict:
    return {
        "keyword": "cordless drill",
        "total_products": 825,
        "used_nav_param": "N-5yc1vZc27f",
        "products": products
        if products is not None
        else [
            {
                "item_id": "315994093",
                "name": "Atomic 20V Max Cordless Drill/Driver",
                "brand": "DEWALT",
                "model_number": "DCD794B",
                "price_dollars": 99.0,
                "was_price_dollars": 129.0,
                "in_stock": True,
                "stock_quantity": 12,
                "rating": 4.7,
                "review_count": 250,
                "product_url": "https://www.homedepot.com/p/315994093",
                "image_url": "https://images.thdstatic.com/a_400.jpg",
            }
        ],
    }


def _client_returning(response: httpx.Response) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _response(status: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=json_body,
        request=httpx.Request("GET", f"{SIDECAR_URL}/search"),
    )


class TestSidecarClient:
    @pytest.mark.asyncio
    async def test_search_maps_products(self) -> None:
        client = _client_returning(_response(200, _payload()))
        supplier = SidecarSupplier(SIDECAR_URL)

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            results = await supplier.search_products("cordless drill", Location(zip_code="30301"))

        assert len(results) == 1
        product = results[0]
        assert product.supplier == "homedepot"
        assert product.product_id == "315994093"
        assert product.brand == "DEWALT"
        assert product.price_dollars == 99.0
        assert product.was_price_dollars == 129.0
        assert product.in_stock is True
        assert product.stock_quantity == 12
        assert product.rating == 4.7

    @pytest.mark.asyncio
    async def test_search_forwards_query_params(self) -> None:
        client = _client_returning(_response(200, _payload()))
        supplier = SidecarSupplier(SIDECAR_URL)

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            await supplier.search_products(
                "drill", Location(zip_code="30301", store_id="4136"), max_results=3
            )

        params = client.get.call_args.kwargs["params"]
        assert params == {
            "q": "drill",
            "site": "home_depot",
            "zip": "30301",
            "store_id": "4136",
            "limit": "3",
        }

    @pytest.mark.asyncio
    async def test_bearer_token_is_sent_when_configured(self) -> None:
        client = _client_returning(_response(200, _payload()))
        supplier = SidecarSupplier(SIDECAR_URL, token="s3cret")

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            await supplier.search_products("drill", Location(zip_code="30301"))

        assert client.get.call_args.kwargs["headers"] == {"authorization": "Bearer s3cret"}

    @pytest.mark.asyncio
    async def test_no_auth_header_without_token(self) -> None:
        client = _client_returning(_response(200, _payload()))
        supplier = SidecarSupplier(SIDECAR_URL)

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            await supplier.search_products("drill", Location(zip_code="30301"))

        assert client.get.call_args.kwargs["headers"] == {}

    @pytest.mark.asyncio
    async def test_trailing_slash_in_base_url_is_normalised(self) -> None:
        client = _client_returning(_response(200, _payload()))
        supplier = SidecarSupplier(SIDECAR_URL + "/")

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            await supplier.search_products("drill", Location(zip_code="30301"))

        assert client.get.call_args.args[0] == f"{SIDECAR_URL}/search"

    @pytest.mark.asyncio
    async def test_truncates_to_max_results(self) -> None:
        many = [dict(_payload()["products"][0], item_id=str(i)) for i in range(10)]
        client = _client_returning(_response(200, _payload(many)))
        supplier = SidecarSupplier(SIDECAR_URL)

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            results = await supplier.search_products(
                "drill", Location(zip_code="30301"), max_results=2
            )

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_unreachable_sidecar_raises_blocked(self) -> None:
        """A down sidecar must look like a block so the caller falls through."""
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        supplier = SidecarSupplier(SIDECAR_URL)

        with (
            patch(
                "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
                return_value=client,
            ),
            pytest.raises(SupplierUnavailableError),
        ):
            await supplier.search_products("drill", Location(zip_code="30301"))

    @pytest.mark.asyncio
    async def test_timeout_names_itself_in_the_error(self) -> None:
        """Regression for #1496.

        `httpx.TimeoutException` subclasses `HTTPError`, so the catch-all clause
        used to swallow it and report a timed-out sidecar as "unreachable". The
        two mean different things operationally and the message has to keep them
        apart. It stays a SupplierUnavailableError either way, so the caller
        still falls through to the next backend.
        """
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ReadTimeout("too slow"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        supplier = SidecarSupplier(SIDECAR_URL, timeout_seconds=35.0)

        with (
            patch(
                "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
                return_value=client,
            ),
            pytest.raises(SupplierUnavailableError, match="timed out after 35s"),
        ):
            await supplier.search_products("drill", Location(zip_code="30301"))

    @pytest.mark.asyncio
    async def test_error_status_raises_blocked(self) -> None:
        client = _client_returning(_response(502, {"detail": "browser died"}))
        supplier = SidecarSupplier(SIDECAR_URL)

        with (
            patch(
                "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
                return_value=client,
            ),
            pytest.raises(SupplierUnavailableError),
        ):
            await supplier.search_products("drill", Location(zip_code="30301"))

    @pytest.mark.asyncio
    async def test_healthy_reflects_sidecar_state(self) -> None:
        supplier = SidecarSupplier(SIDECAR_URL)
        for body, expected in (({"ok": True}, True), ({"ok": False}, False)):
            client = _client_returning(_response(200, body))
            with patch(
                "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
                return_value=client,
            ):
                assert await supplier.healthy() is expected

    @pytest.mark.asyncio
    async def test_healthy_is_false_when_unreachable(self) -> None:
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        supplier = SidecarSupplier(SIDECAR_URL)

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            assert await supplier.healthy() is False


class TestBackendChain:
    """sidecar -> direct -> SerpApi, first one that answers wins."""

    @staticmethod
    def _tools(sidecar: object, serpapi: object) -> dict[str, Callable[..., Awaitable[ToolResult]]]:
        sidecars = {"home_depot": sidecar} if sidecar is not None else {}
        # Fresh caches per tool set: a shared outage counter would let one test's
        # failures suppress the next test's search.
        tools = _create_pricing_tools(
            sidecars,  # type: ignore[arg-type]
            serpapi,  # type: ignore[arg-type]
            SupplierCache(),
            SupplierCache(),
        )
        return {t.name: t.function for t in tools}

    @classmethod
    def _search_tool(cls, sidecar: object, serpapi: object) -> Callable[..., Awaitable[ToolResult]]:
        return cls._tools(sidecar, serpapi)["supplier_search_products"]

    @staticmethod
    def _backend(name: str, blocked: bool = False) -> AsyncMock:
        backend = AsyncMock()
        if blocked:
            backend.search_products = AsyncMock(side_effect=SupplierUnavailableError(name))
        else:
            backend.search_products = AsyncMock(
                return_value=[ProductResult(supplier="homedepot", product_id="1", name=name)]
            )
        return backend

    @pytest.mark.asyncio
    async def test_sidecar_wins_when_healthy(self) -> None:
        sidecar, serpapi = self._backend("from-sidecar"), self._backend("from-serpapi")
        search = self._search_tool(sidecar, serpapi)

        result = await search(query="drill", zip_code="30301")

        assert "from-sidecar" in result.content
        serpapi.search_products.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_through_to_serpapi(self) -> None:
        sidecar, serpapi = self._backend("sidecar", blocked=True), self._backend("from-serpapi")
        search = self._search_tool(sidecar, serpapi)

        result = await search(query="drill", zip_code="30301")

        assert not result.is_error
        assert "from-serpapi" in result.content
        serpapi.search_products.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_every_backend_unavailable_is_a_service_error(self) -> None:
        search = self._search_tool(
            self._backend("sidecar", blocked=True), self._backend("serpapi", blocked=True)
        )

        result = await search(query="drill", zip_code="30301")

        assert result.is_error
        assert result.error_kind is ToolErrorKind.SERVICE

    @pytest.mark.asyncio
    async def test_sidecar_alone_is_enough(self) -> None:
        search = self._search_tool(self._backend("only-sidecar"), None)

        result = await search(query="drill", zip_code="30301")

        assert "only-sidecar" in result.content

    @pytest.mark.asyncio
    async def test_store_lookup_uses_the_sidecar(self) -> None:
        sidecar = self._backend("sidecar")
        sidecar.find_stores = AsyncMock(
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

        result = await self._tools(sidecar, None)["supplier_find_stores"](near="30301")

        assert not result.is_error
        assert "store #0159" in result.content
        assert "1 Example Ave, Springfield, GA 30308" in result.content

    @pytest.mark.asyncio
    async def test_store_lookup_without_a_sidecar_is_unavailable(self) -> None:
        """SerpApi has no store endpoint, so a missing sidecar means no stores."""
        result = await self._tools(None, self._backend("serpapi"))["supplier_find_stores"](
            near="30301"
        )

        assert result.is_error
        assert result.error_kind is ToolErrorKind.SERVICE
        assert "not available" in result.content

    @pytest.mark.asyncio
    async def test_store_lookup_surfaces_sidecar_failure(self) -> None:
        sidecar = self._backend("sidecar")
        sidecar.find_stores = AsyncMock(side_effect=SupplierUnavailableError("down"))

        result = await self._tools(sidecar, None)["supplier_find_stores"](near="30301")

        assert result.is_error
        assert result.error_kind is ToolErrorKind.SERVICE


class TestLowesRouting:
    """Lowe's shares the client and sidecar; only the `site` parameter differs."""

    @staticmethod
    def _tools(sidecars: dict, serpapi: object) -> dict[str, Callable[..., Awaitable[ToolResult]]]:
        tools = _create_pricing_tools(
            sidecars,
            serpapi,  # type: ignore[arg-type]
            SupplierCache(),
            SupplierCache(),
        )
        return {t.name: t.function for t in tools}

    @staticmethod
    def _backend(name: str, blocked: bool = False) -> AsyncMock:
        backend = AsyncMock()
        if blocked:
            backend.search_products = AsyncMock(side_effect=SupplierUnavailableError(name))
        else:
            backend.search_products = AsyncMock(
                return_value=[ProductResult(supplier=name, product_id="1", name=f"{name}-item")]
            )
        return backend

    @pytest.mark.asyncio
    async def test_site_is_forwarded_to_the_sidecar(self) -> None:
        client = _client_returning(_response(200, _payload()))
        supplier = SidecarSupplier(SIDECAR_URL, site="lowes", name="lowes", display_name="Lowe's")

        with patch(
            "backend.app.integrations.supplier_pricing.sidecar_client.httpx.AsyncClient",
            return_value=client,
        ):
            results = await supplier.search_products("drill", Location(zip_code="30301"))

        assert client.get.call_args.kwargs["params"]["site"] == "lowes"
        # Results are attributed to the retailer that produced them.
        assert results[0].supplier == "lowes"

    @pytest.mark.asyncio
    async def test_supplier_argument_selects_the_backend(self) -> None:
        hd, lowes = self._backend("homedepot"), self._backend("lowes")
        search = self._tools({"home_depot": hd, "lowes": lowes}, None)["supplier_search_products"]

        await search(query="drill", zip_code="30301", supplier="lowes")

        lowes.search_products.assert_awaited_once()
        hd.search_products.assert_not_called()

    @pytest.mark.asyncio
    async def test_lowes_does_not_fall_back_to_serpapi(self) -> None:
        """SerpApi has no Lowe's engine, so falling back would return the wrong retailer."""
        lowes = self._backend("lowes", blocked=True)
        serpapi = self._backend("homedepot")
        search = self._tools({"lowes": lowes}, serpapi)["supplier_search_products"]

        result = await search(query="drill", zip_code="30301", supplier="lowes")

        assert result.is_error
        assert result.error_kind is ToolErrorKind.SERVICE
        serpapi.search_products.assert_not_called()

    @pytest.mark.asyncio
    async def test_home_depot_still_falls_back_to_serpapi(self) -> None:
        hd = self._backend("homedepot", blocked=True)
        serpapi = self._backend("from-serpapi")
        search = self._tools({"home_depot": hd}, serpapi)["supplier_search_products"]

        result = await search(query="drill", zip_code="30301", supplier="home_depot")

        assert not result.is_error
        serpapi.search_products.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retailer_name_appears_in_output(self) -> None:
        search = self._tools({"lowes": self._backend("lowes")}, None)["supplier_search_products"]

        result = await search(query="drill", zip_code="30301", supplier="lowes")

        assert "Lowe's" in result.content

    @pytest.mark.asyncio
    async def test_cache_is_keyed_per_retailer(self) -> None:
        """The same query at two retailers must not share a cached answer."""
        hd, lowes = self._backend("homedepot"), self._backend("lowes")
        search = self._tools({"home_depot": hd, "lowes": lowes}, None)["supplier_search_products"]

        await search(query="drill", zip_code="30301", supplier="home_depot")
        await search(query="drill", zip_code="30301", supplier="lowes")

        hd.search_products.assert_awaited_once()
        lowes.search_products.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lowes_results_are_not_labelled_with_the_users_zip(self) -> None:
        """Lowe's ignores the zip, so claiming it would mislead about local stock."""
        lowes = self._backend("lowes")
        lowes.search_products = AsyncMock(
            return_value=[
                ProductResult(
                    supplier="lowes",
                    product_id="1",
                    name="Joint Compound",
                    price_dollars=13.26,
                    in_stock=True,
                    stock_quantity=106,
                )
            ]
        )
        search = self._tools({"lowes": lowes}, None)["supplier_search_products"]

        result = await search(query="joint compound", zip_code="30301", supplier="lowes")

        assert "zip 30301" not in result.content
        assert "not localized to your zip" in result.content

    @pytest.mark.asyncio
    async def test_home_depot_results_keep_the_zip_label(self) -> None:
        hd = self._backend("homedepot")
        hd.search_products = AsyncMock(
            return_value=[
                ProductResult(
                    supplier="homedepot",
                    product_id="1",
                    name="Joint Compound",
                    price_dollars=6.98,
                    in_stock=True,
                    stock_quantity=24,
                )
            ]
        )
        search = self._tools({"home_depot": hd}, None)["supplier_search_products"]

        result = await search(query="joint compound", zip_code="30301", supplier="home_depot")

        assert "zip 30301" in result.content
        assert "not localized" not in result.content

    @pytest.mark.asyncio
    async def test_lowes_cache_ignores_the_zip(self) -> None:
        """The zip cannot change a Lowe's answer, so it must not fragment the cache."""
        lowes = self._backend("lowes")
        search = self._tools({"lowes": lowes}, None)["supplier_search_products"]

        await search(query="drill", zip_code="30301", supplier="lowes")
        await search(query="drill", zip_code="99999", supplier="lowes")

        lowes.search_products.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_serpapi_alone_cannot_serve_a_lowes_request(self) -> None:
        """Falling back would return Home Depot prices labelled as Lowe's."""
        serpapi = self._backend("homedepot")
        search = self._tools({}, serpapi)["supplier_search_products"]

        result = await search(query="drill", zip_code="30301", supplier="lowes")

        assert result.is_error
        serpapi.search_products.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_lookup_uses_home_depot_even_when_lowes_present(self) -> None:
        hd, lowes = self._backend("homedepot"), self._backend("lowes")
        hd.find_stores = AsyncMock(return_value=[])
        lowes.find_stores = AsyncMock(return_value=[])
        tools = self._tools({"home_depot": hd, "lowes": lowes}, None)

        await tools["supplier_find_stores"](near="30301")

        hd.find_stores.assert_awaited_once()
        lowes.find_stores.assert_not_called()


class TestFactoryBuildsChain:
    @staticmethod
    def _settings(**overrides: object) -> MagicMock:
        settings = MagicMock()
        settings.home_depot_sidecar_url = ""
        settings.home_depot_sidecar_token = ""
        settings.serpapi_api_key = ""
        for key, value in overrides.items():
            setattr(settings, key, value)
        return settings

    def test_sidecar_url_enables_the_tools(self) -> None:
        with patch(
            "backend.app.integrations.supplier_pricing.factory.settings",
            self._settings(home_depot_sidecar_url=SIDECAR_URL),
        ):
            tools = _pricing_factory(MagicMock())

        assert [t.name for t in tools] == ["supplier_search_products", "supplier_find_stores"]

    def test_no_backends_yields_no_tools(self) -> None:
        with patch(
            "backend.app.integrations.supplier_pricing.factory.settings",
            self._settings(),
        ):
            assert _pricing_factory(MagicMock()) == []

    def test_serpapi_alone_still_enables_product_search(self) -> None:
        """A SerpApi key with no sidecar keeps product search working."""
        with patch(
            "backend.app.integrations.supplier_pricing.factory.settings",
            self._settings(serpapi_api_key="key"),
        ):
            tools = _pricing_factory(MagicMock())

        assert [t.name for t in tools] == ["supplier_search_products", "supplier_find_stores"]
