"""Pluggable supplier pricing integrations."""

from backend.app.integrations.supplier_pricing.cache import SupplierCache
from backend.app.integrations.supplier_pricing.homedepot import HomeDepotSupplier
from backend.app.integrations.supplier_pricing.protocol import (
    Location,
    ProductDetails,
    ProductResult,
    SupplierBackend,
)

__all__ = [
    "HomeDepotSupplier",
    "Location",
    "ProductDetails",
    "ProductResult",
    "SupplierBackend",
    "SupplierCache",
]
