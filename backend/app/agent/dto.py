"""Pydantic data transfer objects (DTOs) and utility functions.

These models serve as in-memory representations, decoupled from the ORM models
in backend.app.models.
"""

from __future__ import annotations

import datetime
import re

from pydantic import BaseModel, Field

from backend.app.config import settings
from backend.app.enums import EstimateStatus, InvoiceStatus


class UserData(BaseModel):
    """In-memory user DTO."""

    id: str = ""
    user_id: str = ""
    phone: str = ""
    soul_text: str = ""
    user_text: str = ""
    heartbeat_text: str = ""
    timezone: str = ""
    preferred_channel: str = Field(default_factory=lambda: settings.messaging_provider)
    channel_identifier: str = ""
    onboarding_complete: bool = False
    is_active: bool = True
    heartbeat_opt_in: bool = True
    heartbeat_frequency: str = Field(default_factory=lambda: settings.heartbeat_default_frequency)
    folder_scheme: str = Field(default_factory=lambda: settings.default_folder_scheme)
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    updated_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


class StoredMessage(BaseModel):
    """In-memory message DTO. One line in a session JSONL file."""

    direction: str = ""
    body: str = ""
    processed_context: str = ""
    tool_interactions_json: str = ""
    external_message_id: str = ""
    media_urls_json: str = "[]"
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    seq: int = 0


class SessionMetadata(BaseModel):
    """First line of a session JSONL file."""

    session_id: str = ""
    user_id: str = ""
    last_message_at: str = ""
    is_active: bool = True
    last_compacted_seq: int = 0
    channel: str = ""


class SessionState(BaseModel):
    """In-memory representation of a conversation session."""

    session_id: str = ""
    user_id: str = ""
    messages: list[StoredMessage] = Field(default_factory=list)
    is_active: bool = True
    created_at: str = ""
    last_message_at: str = ""
    last_compacted_seq: int = 0
    channel: str = ""


class ClientData(BaseModel):
    """In-memory client DTO."""

    id: str = ""
    name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    notes: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class EstimateLineItemData(BaseModel):
    """In-memory estimate line item DTO."""

    id: str = ""
    description: str = ""
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0


class EstimateData(BaseModel):
    """In-memory estimate DTO."""

    id: str = ""
    user_id: str = ""
    client_id: str | None = None
    description: str = ""
    total_amount: float = 0.0
    status: str = EstimateStatus.DRAFT
    pdf_url: str = ""
    storage_path: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    line_items: list[EstimateLineItemData] = Field(default_factory=list)


class InvoiceLineItemData(BaseModel):
    """In-memory invoice line item DTO."""

    id: str = ""
    description: str = ""
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0


class InvoiceData(BaseModel):
    """In-memory invoice DTO."""

    id: str = ""
    user_id: str = ""
    client_id: str | None = None
    description: str = ""
    total_amount: float = 0.0
    status: str = InvoiceStatus.DRAFT
    pdf_url: str = ""
    storage_path: str = ""
    due_date: str | None = None
    estimate_id: str | None = None
    notes: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    line_items: list[InvoiceLineItemData] = Field(default_factory=list)


class MediaData(BaseModel):
    """In-memory media file DTO."""

    id: str = ""
    message_id: str | None = None
    user_id: str = ""
    original_url: str = ""
    mime_type: str = ""
    processed_text: str = ""
    storage_url: str = ""
    storage_path: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class HeartbeatLogEntry(BaseModel):
    """Heartbeat log entry DTO."""

    user_id: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class ToolConfigEntry(BaseModel):
    """A single tool group configuration entry."""

    name: str = ""
    description: str = ""
    category: str = "domain"
    domain_group: str = ""
    domain_group_order: int = 0
    enabled: bool = True
    auto_disabled_reason: str | None = None


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def slugify(text: str, max_length: int = 60) -> str:
    """Convert text to a filesystem-safe slug.

    Example: "John Smith - 116 Virginia Ave" -> "john_smith_116_virginia_ave"
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:max_length].rstrip("_")


def _unique_slug(base_slug: str, existing_ids: set[str]) -> str:
    """Return a unique slug by appending a counter suffix if needed."""
    if base_slug not in existing_ids:
        return base_slug
    counter = 2
    while f"{base_slug}_{counter}" in existing_ids:
        counter += 1
    return f"{base_slug}_{counter}"


def make_client_slug(
    name: str = "",
    address: str = "",
    folder_scheme: str = "",
) -> str:
    """Build a client slug based on the folder scheme preference."""
    folder_scheme = folder_scheme or settings.default_folder_scheme
    if folder_scheme == "by_address" and address.strip():
        return slugify(address)
    if folder_scheme == "by_client_and_address":
        parts = []
        if name.strip():
            parts.append(name.strip())
        if address.strip():
            parts.append(address.strip())
        if parts:
            return slugify(" ".join(parts))
    if name.strip():
        return slugify(name)
    if address.strip():
        return slugify(address)
    return ""
