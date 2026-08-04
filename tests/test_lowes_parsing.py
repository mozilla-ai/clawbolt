"""Tests for the Lowe's preloaded-state parser.

The parser lives in the sidecar (``sidecar/home_depot/lowes.py``), which is
outside the installed package because it carries the browser stack. It is
imported here by path so its pure functions still get covered: they are the part
most likely to break when Lowe's changes its payload, and they need no browser.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sidecar" / "home_depot" / "lowes.py"


def _load_lowes() -> Any:
    spec = importlib.util.spec_from_file_location("sidecar_lowes", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lowes = _load_lowes()


def _state(items: list[dict] | None = None, count: int = 39) -> dict:
    if items is None:
        items = [
            {
                "product": {
                    "omniItemId": "3009537",
                    "description": "3.5 Quarts Premixed All-purpose Drywall Joint Compound",
                    "brand": "SHEETROCK Brand",
                    "modelId": "385140",
                    "pdURL": "/pd/SHEETROCK-Brand-3-5-Quart/3009537",
                    "rating": 4.7,
                    "reviewCount": 1284,
                    "imageUrl": "https://mobileimages.lowes.com/a.jpg",
                },
                "location": {
                    "price": {"sellingPrice": 13.26},
                    "itemInventory": {
                        "itemAvailList": [
                            {"fullMtdMsg": "Parcel", "isAvlSts": False, "onhandQty": 0},
                            {"fullMtdMsg": "Pickup", "isAvlSts": True, "onhandQty": 106},
                        ]
                    },
                },
            }
        ]
    return {"itemCount": count, "itemList": items}


def _page(state: dict) -> str:
    return (
        "<html><body><script>window.Lowes = {}</script>"
        f"<script>window['__PRELOADED_STATE__'] = {json.dumps(state)};</script>"
        "</body></html>"
    )


class TestExtractPreloadedState:
    def test_extracts_the_payload(self) -> None:
        got = lowes.extract_preloaded_state(_page(_state()))
        assert got is not None
        assert got["itemCount"] == 39

    def test_returns_none_without_the_marker(self) -> None:
        assert lowes.extract_preloaded_state("<html><body>nothing here</body></html>") is None

    def test_braces_inside_strings_do_not_end_the_object(self) -> None:
        """Brace matching has to ignore braces that live inside JSON strings."""
        state = _state()
        state["itemList"][0]["product"]["description"] = 'Weird }{ name with "quotes" and {braces}'
        got = lowes.extract_preloaded_state(_page(state))
        assert got is not None
        assert got["itemList"][0]["product"]["description"].startswith("Weird }{")

    def test_trailing_markup_after_the_object_is_ignored(self) -> None:
        html = _page(_state()) + "<script>window.other = {a: 1}</script>"
        got = lowes.extract_preloaded_state(html)
        assert got is not None and got["itemCount"] == 39

    def test_returns_none_on_unparseable_payload(self) -> None:
        html = "<script>window['__PRELOADED_STATE__'] = {not valid json,,};</script>"
        assert lowes.extract_preloaded_state(html) is None


class TestParseProducts:
    def test_maps_every_field(self) -> None:
        product = lowes.parse_products(_state(), 5)[0]
        assert product["item_id"] == "3009537"
        assert product["brand"] == "SHEETROCK Brand"
        assert product["model_number"] == "385140"
        assert product["price_dollars"] == 13.26
        assert product["rating"] == 4.7
        assert product["review_count"] == 1284
        assert product["product_url"] == (
            "https://www.lowes.com/pd/SHEETROCK-Brand-3-5-Quart/3009537"
        )

    def test_stock_takes_the_best_fulfilment_method(self) -> None:
        """Availability is per method; any available method means in stock."""
        product = lowes.parse_products(_state(), 5)[0]
        assert product["in_stock"] is True
        assert product["stock_quantity"] == 106

    def test_out_of_stock_when_no_method_is_available(self) -> None:
        state = _state()
        state["itemList"][0]["location"]["itemInventory"]["itemAvailList"] = [
            {"fullMtdMsg": "Pickup", "isAvlSts": False, "onhandQty": 0}
        ]
        product = lowes.parse_products(state, 5)[0]
        assert product["in_stock"] is False

    def test_unknown_stock_when_inventory_is_absent(self) -> None:
        state = _state()
        state["itemList"][0]["location"].pop("itemInventory")
        product = lowes.parse_products(state, 5)[0]
        assert product["in_stock"] is None
        assert product["stock_quantity"] is None

    def test_missing_price_is_none_rather_than_zero(self) -> None:
        state = _state()
        state["itemList"][0]["location"]["price"] = {}
        assert lowes.parse_products(state, 5)[0]["price_dollars"] is None

    def test_minimal_entry_does_not_raise(self) -> None:
        product = lowes.parse_products({"itemList": [{}]}, 5)[0]
        assert product["name"] == "Unknown product"
        assert product["price_dollars"] is None

    def test_respects_the_limit(self) -> None:
        items = _state()["itemList"] * 10
        assert len(lowes.parse_products({"itemList": items}, 3)) == 3

    def test_empty_item_list(self) -> None:
        assert lowes.parse_products({"itemList": []}, 5) == []


class TestPayloadShapeChanges:
    """Guards for the ways a payload change could break this quietly."""

    @pytest.mark.parametrize(
        "assignment",
        [
            "window['__PRELOADED_STATE__'] = ",
            'window["__PRELOADED_STATE__"] = ',
            "window.__PRELOADED_STATE__ = ",
        ],
    )
    def test_all_assignment_forms_are_accepted(self, assignment: str) -> None:
        """A minifier switching quote style must not silently break search."""
        html = f"<script>{assignment}{json.dumps(_state())};</script>"
        got = lowes.extract_preloaded_state(html)
        assert got is not None and got["itemCount"] == 39

    @pytest.mark.parametrize("bad", ["not-a-number", None, True, [], {}])
    def test_total_products_coerces_unusable_values_to_none(self, bad: object) -> None:
        """Reaches a pydantic model, so junk must not 500 the sidecar."""
        assert lowes.total_products({"itemCount": bad}) is None

    def test_total_products_accepts_a_numeric_string(self) -> None:
        """Lowe's ships some numbers as strings, so those must survive."""
        assert lowes.total_products({"itemCount": "39"}) == 39

    def test_total_products_reads_a_real_count(self) -> None:
        assert lowes.total_products({"itemCount": 39}) == 39

    @pytest.mark.parametrize("bad", ["n/a", None, True, {}])
    def test_rating_and_reviews_coerce_unusable_values(self, bad: object) -> None:
        state = _state()
        state["itemList"][0]["product"]["rating"] = bad
        state["itemList"][0]["product"]["reviewCount"] = bad
        product = lowes.parse_products(state, 5)[0]
        assert product["rating"] is None
        assert product["review_count"] is None

    def test_rating_and_reviews_arrive_as_strings_in_the_real_payload(self) -> None:
        """Lowe's really does send these as strings; rejecting them lost real data."""
        state = _state()
        state["itemList"][0]["product"]["rating"] = "4.7"
        state["itemList"][0]["product"]["reviewCount"] = "4076"
        product = lowes.parse_products(state, 5)[0]
        assert product["rating"] == 4.7
        assert product["review_count"] == 4076

    def test_stock_quantity_survives_a_string_count(self) -> None:
        state = _state()
        state["itemList"][0]["location"]["itemInventory"]["itemAvailList"] = [
            {"fullMtdMsg": "Pickup", "isAvlSts": True, "onhandQty": "106"}
        ]
        assert lowes.parse_products(state, 5)[0]["stock_quantity"] == 106

    def test_review_count_from_a_float_becomes_an_int(self) -> None:
        state = _state()
        state["itemList"][0]["product"]["reviewCount"] = 1284.0
        assert lowes.parse_products(state, 5)[0]["review_count"] == 1284


class TestHelpers:
    @pytest.mark.parametrize(
        "keyword,expected",
        [("putty knife", "searchTerm=putty+knife"), ("1/2 copper", "searchTerm=1%2F2+copper")],
    )
    def test_search_url_encodes_the_keyword(self, keyword: str, expected: str) -> None:
        assert expected in lowes.search_url(keyword)

    def test_is_denied_detects_the_edge_refusal(self) -> None:
        assert lowes.is_denied("<H1>Access Denied</H1>") is True
        assert lowes.is_denied(_page(_state())) is False
