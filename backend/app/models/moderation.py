"""Conversations a user reported for operator review."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base

if TYPE_CHECKING:
    # Relationship targets resolve through SQLAlchemy's class registry
    # at mapper-configuration time, so these are annotations only.
    from backend.app.models.user import User


class ReportedConversation(Base):
    """A user-initiated report flagging a conversation for admin review.

    Created when a user texts ``/report [reason]`` to the bot through any
    channel. The user is signaling that something happened in this
    conversation they want a human to look at: the bot misbehaved, gave
    wrong information, said something distressing, etc.

    The premium ``/admin/reported-conversations`` router consumes these
    rows. ``anchor_seq`` records which inbound message triggered the
    report so the admin UI can highlight the surrounding conversation
    window. ``dismissed_at`` + ``reviewed_admin_user_id`` are set when an
    admin closes out the report. ``reason`` is the optional free-text
    that followed ``/report`` (empty string if the user just sent
    ``/report`` alone).
    """

    __tablename__ = "reported_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The seq of the inbound message whose ``/report`` text created this
    # row. Nullable because future programmatic reports (e.g. an admin
    # tool that flags a conversation without an originating message)
    # might not have one.
    anchor_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    # Admin review state. ``dismissed_at`` is the time the report was
    # closed; ``reviewed_admin_user_id`` is the admin who closed it.
    # Both NULL until a review happens. ``ondelete=SET NULL`` on the
    # admin FK so deleting an admin doesn't cascade-delete reports.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_admin_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Reverse side of ``User.reported_conversations``. Disambiguated by
    # ``foreign_keys`` because this table has two FKs to ``users.id``.
    user: Mapped[User] = relationship(
        "User",
        back_populates="reported_conversations",
        foreign_keys=[user_id],
        lazy="raise",
    )
