"""SQLAlchemy ORM models for clawbolt."""

import uuid as _uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .database import Base


class User(Base):
    """A Clawbolt user.

    **Dual channel identity system:**

    Channel identity lives in two places that serve different purposes:

    * ``ChannelRoute`` (separate table) -- authoritative mapping of
      ``(channel, channel_identifier)`` to a user.  Used for inbound
      routing and allowlist checks.  Supports multiple channels per user.

    * ``User.channel_identifier`` / ``User.preferred_channel`` -- cached
      shortcut to the user's most-recently-used channel.  Used by
      heartbeat and proactive messaging to quickly determine where to
      deliver messages without joining through ``ChannelRoute``.

    Both are kept in sync by ``_get_or_create_user()`` in ingestion.py.
    When they diverge, ``ChannelRoute`` is authoritative for routing and
    the User-level fields are authoritative for "default delivery channel".
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(_uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    phone: Mapped[str] = mapped_column(String, default="")
    timezone: Mapped[str] = mapped_column(String, default="")
    preferred_channel: Mapped[str] = mapped_column(String, default="telegram")
    channel_identifier: Mapped[str] = mapped_column(String, default="")
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    heartbeat_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    heartbeat_frequency: Mapped[str] = mapped_column(String, default="30m")
    heartbeat_max_daily: Mapped[int] = mapped_column(Integer, default=0)
    soul_text: Mapped[str] = mapped_column(Text, default="")
    user_text: Mapped[str] = mapped_column(Text, default="")
    heartbeat_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def __init__(self, **kwargs: object) -> None:
        # Apply Python-side defaults that mirror settings for columns
        # where the DB default is a static string but the Pydantic UserData
        # used to read from settings dynamically.
        _static_defaults: dict[str, str | bool] = {
            "phone": "",
            "timezone": "",
            "preferred_channel": settings.messaging_provider,
            "channel_identifier": "",
            "onboarding_complete": False,
            "is_active": True,
            "heartbeat_opt_in": True,
            "heartbeat_frequency": settings.heartbeat_default_frequency,
            "soul_text": "",
            "user_text": "",
            "heartbeat_text": "",
        }
        _factory_defaults: dict[str, Callable[[], object]] = {
            "id": lambda: str(_uuid.uuid4()),
            "created_at": lambda: datetime.now(UTC),
            "updated_at": lambda: datetime.now(UTC),
        }
        for key, static in _static_defaults.items():
            if key not in kwargs:
                kwargs[key] = static
        for key, factory in _factory_defaults.items():
            if key not in kwargs:
                kwargs[key] = factory()
        super().__init__(**kwargs)

    channel_routes: Mapped[list["ChannelRoute"]] = relationship(
        "ChannelRoute", back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["ChatSession"]] = relationship(
        "ChatSession", back_populates="user", cascade="all, delete-orphan"
    )
    media_files: Mapped[list["MediaFile"]] = relationship(
        "MediaFile", back_populates="user", cascade="all, delete-orphan"
    )
    memory_documents: Mapped[list["MemoryDocument"]] = relationship(
        "MemoryDocument", back_populates="user", cascade="all, delete-orphan"
    )
    heartbeat_logs: Mapped[list["HeartbeatLog"]] = relationship(
        "HeartbeatLog", back_populates="user", cascade="all, delete-orphan"
    )
    llm_usage_logs: Mapped[list["LLMUsageLog"]] = relationship(
        "LLMUsageLog", back_populates="user", cascade="all, delete-orphan"
    )
    tool_configs: Mapped[list["ToolConfig"]] = relationship(
        "ToolConfig", back_populates="user", cascade="all, delete-orphan"
    )
    calendar_configs: Mapped[list["CalendarConfig"]] = relationship(
        "CalendarConfig", back_populates="user", cascade="all, delete-orphan"
    )


class ChannelRoute(Base):
    __tablename__ = "channel_routes"
    __table_args__ = (UniqueConstraint("channel", "channel_identifier", name="uq_channel_route"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String, nullable=False)
    channel_identifier: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="channel_routes")


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    channel: Mapped[str] = mapped_column(String, default="")
    last_compacted_seq: Mapped[int] = mapped_column(Integer, default=0)
    initial_system_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="sessions")
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_message_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    processed_context: Mapped[str] = mapped_column(Text, default="")
    tool_interactions_json: Mapped[str] = mapped_column(Text, default="")
    external_message_id: Mapped[str] = mapped_column(String, default="")
    media_urls_json: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[str] = mapped_column(String, default="")
    original_url: Mapped[str] = mapped_column(Text, default="")
    mime_type: Mapped[str] = mapped_column(String, default="")
    processed_text: Mapped[str] = mapped_column(Text, default="")
    storage_url: Mapped[str] = mapped_column(Text, default="")
    storage_path: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="media_files")


class MemoryDocument(Base):
    __tablename__ = "memory_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    memory_text: Mapped[str] = mapped_column(Text, default="")
    history_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship("User", back_populates="memory_documents")


class HeartbeatLog(Base):
    __tablename__ = "heartbeat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(String, default="send")
    message_text: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    tasks: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="heartbeat_logs")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class LLMUsageLog(Base):
    __tablename__ = "llm_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0.000000"))
    purpose: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="llm_usage_logs")


class CalendarConfig(Base):
    __tablename__ = "calendar_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_calendar_config_user_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, default="")
    calendar_id: Mapped[str] = mapped_column(String, default="primary")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="calendar_configs")


class ToolConfig(Base):
    __tablename__ = "tool_configs"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tool_config_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String, default="")
    domain_group: Mapped[str] = mapped_column(String, default="")
    domain_group_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_sub_tools: Mapped[str] = mapped_column(Text, default="")

    user: Mapped["User"] = relationship("User", back_populates="tool_configs")
