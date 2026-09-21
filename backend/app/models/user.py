"""The user record and its per-tool permission set."""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.config import settings
from backend.app.database import Base

if TYPE_CHECKING:
    # Relationship targets resolve through SQLAlchemy's class registry
    # at mapper-configuration time, so these are annotations only.
    from backend.app.models.conversation import ChannelRoute, ChatSession, MemoryDocument
    from backend.app.models.heartbeat import HeartbeatLog
    from backend.app.models.integrations import CalendarConfig, OAuthToken, ToolConfig
    from backend.app.models.llm import LLMUsageLog
    from backend.app.models.moderation import ReportedConversation


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
    __table_args__ = (Index("ix_users_last_login_at", "last_login_at"),)

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
    heartbeat_max_daily: Mapped[int] = mapped_column(Integer, default=5)
    soul_text: Mapped[str] = mapped_column(Text, default="")
    user_text: Mapped[str] = mapped_column(Text, default="")
    heartbeat_text: Mapped[str] = mapped_column(Text, default="")
    # User research / data sharing consent. Defaults to False so admins
    # only see message bodies, memory, and other user content for users
    # who explicitly opted in here. ``data_sharing_consent_at`` is set
    # on every change (opt-in AND opt-out) so consent history can be
    # reconstructed by joining against an audit log if needed.
    data_sharing_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_sharing_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Multi-user accounting. Stamped by the login tracker on every sign-in
    # and read by the inactivity sweep, which warns at
    # ``INACTIVE_WARN_MONTHS`` and deletes at ``INACTIVE_DELETE_MONTHS``.
    # Naive (no timezone) to match the column the migration created;
    # writers strip tzinfo from an otherwise-UTC value before binding.
    # Both stay NULL in single-user deployments, where nothing signs in.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    inactivity_warned_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )
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
            "data_sharing_consent": False,
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

    channel_routes: Mapped[list[ChannelRoute]] = relationship(
        "ChannelRoute", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    sessions: Mapped[list[ChatSession]] = relationship(
        "ChatSession", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    memory_documents: Mapped[list[MemoryDocument]] = relationship(
        "MemoryDocument", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    heartbeat_logs: Mapped[list[HeartbeatLog]] = relationship(
        "HeartbeatLog", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    llm_usage_logs: Mapped[list[LLMUsageLog]] = relationship(
        "LLMUsageLog", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    tool_configs: Mapped[list[ToolConfig]] = relationship(
        "ToolConfig", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    calendar_configs: Mapped[list[CalendarConfig]] = relationship(
        "CalendarConfig", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    oauth_tokens: Mapped[list[OAuthToken]] = relationship(
        "OAuthToken", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    # Reports this user filed via ``/report``. ``ReportedConversation``
    # has two FKs to ``users.id`` (``user_id`` for the reporter,
    # ``reviewed_admin_user_id`` for the admin who closed it); the
    # explicit ``foreign_keys=`` disambiguates that this relationship
    # is the reporter side. We don't expose the admin-side relationship
    # because the audit log already lets ops query "what did this admin
    # do" without an ORM round-trip.
    reported_conversations: Mapped[list[ReportedConversation]] = relationship(
        "ReportedConversation",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="ReportedConversation.user_id",
        lazy="raise",
    )


class UserPermissionSet(Base):
    """Per-user tool/resource permission overrides (formerly PERMISSIONS.json).

    Stores the full permissions document as a JSON-encoded string so the
    app-layer shape matches the legacy file format unchanged. One row per
    user; ``ApprovalStore`` reads/writes this instead of the filesystem.

    No FK / ORM relationship to ``User``: the approval store is exercised
    by lightweight unit tests that don't insert a ``User`` row, and
    production uses soft-delete (``is_active = False``) rather than hard
    row deletion, so cascade cleanup would never fire anyway. Orphan
    hygiene, if ever needed, is handled explicitly at the call site.
    """

    __tablename__ = "user_permissions"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
