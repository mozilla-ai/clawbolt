"""Supplier backend protocol and shared data models."""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class Location(BaseModel):
    """User location for localized pricing."""

    zip_code: str
    store_id: str = ""
    """Home Depot store number, when known.

    Used by the direct and sidecar backends; the SerpApi backend ignores it.
    Supplying it sharpens the result: pricing and shelf inventory come back for
    that specific store. Without it Home Depot still reports inventory, but for
    a location it picks itself, so treat stock as store-local only when this is
    set.
    """


class ProductResult(BaseModel):
    """A single product from a supplier search."""

    supplier: str
    product_id: str
    name: str
    brand: str = ""
    price_dollars: float | None = None
    was_price_dollars: float | None = None
    unit: str = "each"
    in_stock: bool | None = None
    stock_quantity: int | None = None
    aisle: str = ""
    product_url: str = ""
    image_url: str = ""
    rating: float | None = None


class ProductDetails(ProductResult):
    """Extended product detail (Phase 1b)."""

    description: str = ""
    specifications: dict[str, str] = {}
    feature_bullets: list[str] = []


class StoreResult(BaseModel):
    """A single retail location."""

    store_id: str
    name: str
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    distance_miles: float | None = None


@runtime_checkable
class SupplierBackend(Protocol):
    """Interface that all supplier integrations must implement."""

    name: str
    display_name: str

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]: ...

    # Phase 1b: add get_product_details() when supplier_product_details tool ships
