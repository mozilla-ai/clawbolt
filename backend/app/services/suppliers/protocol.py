"""Supplier backend protocol and shared data models."""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class Location(BaseModel):
    """User location for localized pricing."""

    zip_code: str


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


@runtime_checkable
class SupplierBackend(Protocol):
    """Interface that all supplier integrations must implement."""

    name: str
    display_name: str

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]: ...

    # Phase 1b: add get_product_details() when supplier_product_details tool ships
