"""Per-user integration state: calendar, tool config, and OAuth tokens."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base
from backend.app.models.types import EncryptedString

if TYPE_CHECKING:
    # Relationship targets resolve through SQLAlchemy's class registry
    # at mapper-configuration time, so these are annotations only.
    from backend.app.models.user import User


class CalendarConfig(Base):
    __tablename__ = "calendar_configs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "calendar_id",
            name="uq_calendar_config_user_provider_calendar",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, default="")
    calendar_id: Mapped[str] = mapped_column(String, default="primary")
    disabled_tools: Mapped[str] = mapped_column(Text, default="")
    access_role: Mapped[str] = mapped_column(String, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Mirrors Google Calendar's ``primary`` flag on the calendarList entry.
    # When the agent fires a calendar tool without an explicit calendar_id
    # and the user has multiple enabled calendars, the row marked
    # is_primary wins. Avoids the "Multiple calendars available, please
    # specify calendar_id" error every time a contractor with crew
    # sub-calendars asks the agent to add an event.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped[User] = relationship("User", back_populates="calendar_configs", lazy="raise")


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

    user: Mapped[User] = relationship("User", back_populates="tool_configs", lazy="raise")


class OAuthToken(Base):
    """Persisted OAuth token for a user-integration pair.

    Sensitive fields (access_token, refresh_token) are envelope-encrypted
    at rest via ``EncryptedString``: per-row DEK wrapped by the configured
    ``KEKProvider`` (OSS: ``LocalKEKProvider`` keyed by ``ENCRYPTION_KEY``;
    premium: KMS-backed, per-tenant).
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "integration", name="uq_oauth_token_user_integration"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    integration: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[str] = mapped_column(
        EncryptedString(table="oauth_tokens", column="access_token"), default=""
    )
    refresh_token: Mapped[str] = mapped_column(
        EncryptedString(table="oauth_tokens", column="refresh_token"), default=""
    )
    token_type: Mapped[str] = mapped_column(String, default="Bearer")
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    realm_id: Mapped[str] = mapped_column(String, default="")
    extra_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship("User", back_populates="oauth_tokens", lazy="raise")
