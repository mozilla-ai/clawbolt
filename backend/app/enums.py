"""Domain enums for status and direction fields."""

from enum import StrEnum


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
