"""Domain enums and literal value sets shared by the ORM and the API schemas."""

from enum import StrEnum
from typing import Literal


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


# How an LLM endpoint is driven. The ORM column, the read schema, and the
# write schema all use these, so the database, the OpenAPI spec, and the
# frontend agree on one set of legal values per field.
CacheControlMode = Literal["auto", "always", "never"]
ReasoningMode = Literal["auto", "thinking", "effort", "none"]
PricingMode = Literal["auto", "unpriced"]
