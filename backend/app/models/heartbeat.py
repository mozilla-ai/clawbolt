"""The proactive-heartbeat send log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base
from backend.app.models.types import EncryptedString

if TYPE_CHECKING:
    # Relationship targets resolve through SQLAlchemy's class registry
    # at mapper-configuration time, so these are annotations only.
    from backend.app.models.user import User


class HeartbeatLog(Base):
    """A single heartbeat scheduler run.

    Three text columns carry user-facing content and are envelope-
    encrypted at rest via ``EncryptedString`` (same pattern as
    ``Message.body``):

    - ``message_text``: the actual proactive message we sent (or would
      have sent, on a skip).
    - ``reasoning``: the LLM's free-text rationale for sending /
      skipping. Often includes user content paraphrased back.
    - ``tasks``: serialized task state the heartbeat was deciding from.
      Contains user-authored task descriptions.

    ``action_type`` and ``channel`` stay plaintext: short enums needed
    for filtering / aggregation, no PII.
    """

    __tablename__ = "heartbeat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(String, default="send")
    message_text: Mapped[str] = mapped_column(
        EncryptedString(table="heartbeat_logs", column="message_text"), default=""
    )
    channel: Mapped[str] = mapped_column(String, default="")
    reasoning: Mapped[str] = mapped_column(
        EncryptedString(table="heartbeat_logs", column="reasoning"), default=""
    )
    tasks: Mapped[str] = mapped_column(
        EncryptedString(table="heartbeat_logs", column="tasks"), default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped[User] = relationship("User", back_populates="heartbeat_logs", lazy="raise")
