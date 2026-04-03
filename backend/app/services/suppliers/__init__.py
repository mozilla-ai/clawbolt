"""Pluggable supplier pricing integrations."""

from backend.app.services.suppliers.cache import SupplierCache
from backend.app.services.suppliers.homedepot import HomeDepotSupplier
from backend.app.services.suppliers.protocol import (
    Location,
    ProductDetails,
    ProductResult,
    SupplierBackend,
)
from backend.app.services.suppliers.sherwinwilliams import SherwinWilliamsSupplier

__all__ = [
    "HomeDepotSupplier",
    "Location",
    "ProductDetails",
    "ProductResult",
    "SherwinWilliamsSupplier",
    "SupplierBackend",
    "SupplierCache",
]
