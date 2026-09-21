"""Per-tenant plans and quotas (AUTH_MODE=multi_user only)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base


class Subscription(Base):
    """Per-user plan, role, and LLM override. One row per user.

    None of the models in this section declares an ORM ``relationship`` to
    ``User``, so SQLAlchemy has no inter-mapper dependency to order a flush
    by and falls back to the mapper sort key, which is alphabetical:
    ``Subscription`` flushes before ``User``. Adding a user and its
    dependent rows in one flush therefore emits the child INSERT first and
    violates the foreign key. Flush the ``User`` first, as
    ``auth/oauth_flow.py`` does.

    The relationships are omitted deliberately. Declaring one changes what
    ``db.delete(user)`` does to these rows, and deletion is already handled
    explicitly by ``services/user_deletion.py``.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), unique=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="user")
    email: Mapped[str] = mapped_column(String(255), default="")
    plan: Mapped[str] = mapped_column(String(20), default="free")
    status: Mapped[str] = mapped_column(String(20), default="active")
    # Per-user LLM override. Empty string means "use the global default"
    # (settings.llm_endpoint / settings.llm_provider / settings.llm_model).
    # Each field can be set independently: e.g. provider="" +
    # model="claude-opus-4-5" keeps the global provider but pins this user
    # to a specific model.
    #
    # ``llm_endpoint_override`` names a row in ``llm_endpoints``. It
    # supersedes ``llm_provider_override`` when both are set, because the
    # endpoint carries its own dialect and a provider chosen next to it
    # would have to agree with that dialect to mean anything.
    llm_endpoint_override: Mapped[str] = mapped_column(String(64), default="")
    llm_provider_override: Mapped[str] = mapped_column(String(64), default="")
    llm_model_override: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UsageQuota(Base):
    """One row per user per calendar month, holding usage and that month's caps.

    Limits are captured at row creation, so a plan change only affects the
    active month if something rewrites the row (see
    ``billing.quota.apply_plan_limits_to_current_quota``).
    """

    __tablename__ = "usage_quotas"
    __table_args__ = (
        UniqueConstraint("user_id", "period_start", name="uq_usage_quotas_user_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    messages_used: Mapped[int] = mapped_column(Integer, default=0)
    messages_limit: Mapped[int] = mapped_column(Integer, default=1000)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    tokens_limit: Mapped[int] = mapped_column(Integer, default=1_000_000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeletedUserUsage(Base):
    """Archived usage for deleted accounts. Prevents quota reset via re-registration."""

    __tablename__ = "deleted_user_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_user_id: Mapped[str] = mapped_column(String(255), index=True)
    plan_at_deletion: Mapped[str] = mapped_column(String(20), default="free")
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
