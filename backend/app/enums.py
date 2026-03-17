"""Domain enums for status and direction fields."""

from enum import StrEnum


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class EstimateStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class HeartbeatStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"


class HeartbeatSchedule(StrEnum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    ONCE = "once"
