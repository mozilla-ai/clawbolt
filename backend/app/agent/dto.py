"""Pydantic data transfer objects (DTOs) and utility functions.

These models serve as in-memory representations, decoupled from the ORM models
in backend.app.models.
"""

from __future__ import annotations

import datetime
import re

from pydantic import BaseModel, Field

from backend.app.config import settings


def slugify(text: str, max_length: int = 60) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:max_length].rstrip("_")


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
    heartbeat_max_daily: int = 0
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
    initial_system_prompt: str = ""


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
    action_type: str = "send"
    message_text: str = ""
    channel: str = ""
    reasoning: str = ""
    tasks: str = ""
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class SubToolEntry(BaseModel):
    """An individual tool within a tool group."""

    name: str = ""
    description: str = ""
    enabled: bool = True


class ToolConfigEntry(BaseModel):
    """A single tool group configuration entry."""

    name: str = ""
    description: str = ""
    category: str = "domain"
    domain_group: str = ""
    domain_group_order: int = 0
    enabled: bool = True
    sub_tools: list[SubToolEntry] = Field(default_factory=list)
    disabled_sub_tools: list[str] = Field(default_factory=list)
