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


class ChecklistStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class QBEntityType(StrEnum):
    INVOICE = "invoice"
    ESTIMATE = "estimate"


class ChecklistSchedule(StrEnum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    ONCE = "once"
