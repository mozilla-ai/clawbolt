"""Pending tool approvals and the approval lifecycle audit trail."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base


class PendingApprovalRow(Base):
    """In-flight tool approval request, persisted so orphans can be detected
    after a worker crash / restart.

    The running agent coroutine owns the in-memory ``PendingApproval`` and
    waits on an ``asyncio.Event``. If the worker process dies before the
    user replies, that coroutine is gone and can't be resumed. The DB row
    survives, so on startup we can find orphaned requests, send the user a
    recovery message, and clean up instead of silently losing state.

    One row per user (composite key would be excessive: the current gate
    allows only one pending approval per user). A new request overwrites
    an older one via upsert, matching in-memory semantics.

    No FK / ORM relationship to ``User``: approvals are a transient
    recovery aid, not part of the user's durable state, and the startup
    cleanup already drops rows older than ``_ORPHAN_MAX_AGE``. A deleted
    user's row will be swept on the next restart without a cascade.
    """

    __tablename__ = "pending_approvals"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String, default="")
    chat_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ApprovalEvent(Base):
    """Append-only audit log for tool-approval lifecycle transitions.

    Companion to ``PendingApprovalRow``: that row tracks the single
    in-flight request per user and is deleted on resolve, so it cannot
    answer "what was the agent blocked on ten minutes ago?". Each
    transition (``requested``, ``decided``, ``timed_out``, ``recovered``)
    appends one row here so admins can replay the full sequence in the
    activity feed.

    ``decision`` is populated only on ``decided`` rows and carries the
    ``ApprovalDecision`` value (``approved`` / ``denied`` /
    ``always_allow`` / ``always_deny`` / ``interrupted``).

    ``description`` echoes the tool's human-readable description that
    was shown to the user. It can include user-pasted content (filenames,
    URLs, message bodies), so admin surfaces must run it through PII
    redaction before display, the same way ``Message.body`` is handled.

    No retention sweep ships with this table. Volume is small (one row
    per approval transition; an active session generates a handful per
    day) and the data is the audit trail itself, so we let it
    accumulate. Reconsider if a single user crosses ~10k rows.
    """

    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String, default="")
    chat_id: Mapped[str] = mapped_column(String, default="")
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
