"""Chat sessions, messages, working memory, and compaction history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
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
    # Set whenever an inbound message resolves to this route. The channel
    # picker UI reads this field to flip to a "Verified" state so users see
    # that their configured channel actually delivers messages end-to-end.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="channel_routes", lazy="raise")


class ChatSession(Base):
    """Per-user conversation metadata. Exactly one row per user (UNIQUE on
    user_id). Messages link here for ``initial_system_prompt`` capture and
    last-message bookkeeping; there is no concept of multiple sessions
    per user.
    """

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("user_id", name="uq_sessions_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String, default="")
    initial_system_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Highest ``messages.seq`` that has been trimmed out of the LLM context.
    # ``load_conversation_history`` filters to ``seq > last_trim_seq``, so
    # rows below this threshold are no longer fed to the agent. Their durable
    # facts live in MEMORY.md / USER.md / SOUL.md via the compaction path;
    # the original rows remain in the DB for audit. ``NULL`` means nothing
    # has been trimmed yet (default for fresh sessions and pre-feature rows).
    last_trim_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="sessions", lazy="raise")
    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan", lazy="raise"
    )


class Message(Base):
    """A single message in a conversation, inbound or outbound.

    User-authored content (``body``, ``processed_context``,
    ``llm_reply_text``, ``tool_interactions_json``, ``thinking_text``)
    is envelope-encrypted at rest via ``EncryptedString``. ``body`` is
    the raw text the user / channel sent; ``processed_context`` is the
    same content after media transcription / OCR / preprocessing;
    ``tool_interactions_json`` holds tool call args / results that
    frequently embed customer names, phone numbers, and addresses
    passed to QuickBooks / CompanyCam / calendar tools.
    ``thinking_text`` holds the LLM's extended-thinking blocks for
    outbound messages (empty for inbound), which can quote user content
    back at length and so receives the same encryption treatment.
    ``llm_reply_text`` holds the LLM's outbound prose *before* the
    deterministic receipt block was appended; ``body`` is the dispatched
    text the user saw. The history rebuilder feeds ``llm_reply_text`` to
    the LLM on the next turn so the model never trains on its own
    appended receipts (see ``backend/app/agent/context.py``). Empty for
    inbound rows and for outbound rows persisted before migration 037.
    The decrypt path runs transparently on every ORM read, so
    application code keeps reading ``msg.tool_interactions_json`` and
    gets plaintext JSON.

    Other text columns intentionally left plaintext:

    - ``external_message_id``: channel-side ID (Telegram message_id,
      Linq message_id). Not sensitive content; needed in cleartext for
      idempotency-key indexing on inbound webhook retries.
    - ``media_urls_json``: pointers, not bytes.
    """

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_message_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(EncryptedString(table="messages", column="body"), default="")
    processed_context: Mapped[str] = mapped_column(
        EncryptedString(table="messages", column="processed_context"), default=""
    )
    tool_interactions_json: Mapped[str] = mapped_column(
        EncryptedString(table="messages", column="tool_interactions_json"), default=""
    )
    thinking_text: Mapped[str] = mapped_column(
        EncryptedString(table="messages", column="thinking_text"),
        default="",
        server_default="",
    )
    llm_reply_text: Mapped[str] = mapped_column(
        EncryptedString(table="messages", column="llm_reply_text"),
        default="",
        server_default="",
    )
    external_message_id: Mapped[str] = mapped_column(String, default="")
    media_urls_json: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    session: Mapped[ChatSession] = relationship(
        "ChatSession", back_populates="messages", lazy="raise"
    )


class MemoryDocument(Base):
    """Per-user memory document and compaction history.

    ``memory_text`` and ``history_text`` are envelope-encrypted at rest
    via ``EncryptedString`` (same pattern as ``Message.body`` from
    migration 020). This is the user's working memory file (notes,
    reminders, recent context) plus the compacted history of older
    sessions; both contain everything the agent has been told and is
    among the most sensitive content in the database.

    ORM reads decrypt transparently. Direct SQL reads return the
    envelope blob and require ``decrypt()`` to recover plaintext.
    """

    __tablename__ = "memory_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    memory_text: Mapped[str] = mapped_column(
        EncryptedString(table="memory_documents", column="memory_text"), default=""
    )
    history_text: Mapped[str] = mapped_column(
        EncryptedString(table="memory_documents", column="history_text"), default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship("User", back_populates="memory_documents", lazy="raise")


class CompactionEvent(Base):
    """One row per session-compaction run.

    Compaction is the agent's mechanism for trimming a long session and
    extracting durable facts into ``MemoryDocument``. Per-event timing,
    sizes, and outcome flags used to live only in ``logger.info(
    "compaction.summary user=...")`` lines, which made cross-event
    queries ("how often does this user compact? how big are the
    inputs?") impossible without grepping logs.

    All columns are metadata; the actual extracted content (the
    summary appended to ``MemoryDocument.history_text``) stays
    envelope-encrypted at rest under that column. Surfacing this
    table to admins still goes through the consent gate on the
    premium ``/admin/shared-data/users/{id}/compaction-events``
    endpoint; we keep the writes unconditional since the columns
    carry no user-authored content.

    See ``backend/app/agent/compaction.py`` for the call site.
    """

    __tablename__ = "compaction_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    trimmed_count: Mapped[int] = mapped_column(Integer, default=0)
    trimmed_chars: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Range of ``messages.seq`` that this compaction event covers.
    # ``min_message_seq`` is NULL on legacy rows (pre-feature). Going
    # forward, both are populated when the agent loop's trim path inserts
    # the pending row.
    min_message_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_message_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_updated: Mapped[bool] = mapped_column(Boolean, default=False)
    user_profile_updated: Mapped[bool] = mapped_column(Boolean, default=False)
    soul_updated: Mapped[bool] = mapped_column(Boolean, default=False)
    summary_len: Mapped[int] = mapped_column(Integer, default=0)
    # Two-phase lifecycle. The agent loop synchronously inserts a
    # ``'pending'`` row in the same transaction that advances the per-session
    # trim watermark. The async compaction task then runs the LLM call,
    # fills in the snapshot fields below, and flips this to ``'completed'``.
    # If the async task crashes, the row stays ``'pending'`` so an operator
    # can see which seq range was trimmed without facts being extracted, and
    # re-run that compaction manually. Existing pre-feature rows are
    # ``'completed'`` via the server-side default in migration 029.
    status: Mapped[str] = mapped_column(String, default="completed")
    # Startup-sweep retry attempts (see
    # ``backend/app/agent/compaction_recovery.py``). Incremented before
    # each retry's LLM call so a crash mid-retry still counts; rows at
    # the sweep's cap stay ``'pending'`` but stop being selected, so a
    # poisoned range cannot retry forever. Migration 039.
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Before/after snapshots of the four memory files this event touched.
    # Stored as envelope-encrypted text so an admin can inspect what the
    # compaction LLM call actually changed. ``None`` means either the field
    # was not changed by this event (skip-if-unchanged optimization), the
    # row is still ``'pending'``, or the row predates the feature. When the
    # plaintext exceeds ``settings.compaction_event_snapshot_max_bytes_per_file``,
    # the column stores a structured truncation record (head, tail, size,
    # sha256) instead of the full text. Same EncryptedString pattern as
    # ``MemoryDocument.memory_text`` (migration 022).
    memory_text_before: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="memory_text_before"),
        nullable=True,
    )
    memory_text_after: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="memory_text_after"),
        nullable=True,
    )
    history_text_before: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="history_text_before"),
        nullable=True,
    )
    history_text_after: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="history_text_after"),
        nullable=True,
    )
    user_text_before: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="user_text_before"),
        nullable=True,
    )
    user_text_after: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="user_text_after"),
        nullable=True,
    )
    soul_text_before: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="soul_text_before"),
        nullable=True,
    )
    soul_text_after: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="soul_text_after"),
        nullable=True,
    )
    # Capture of the actual compaction LLM call. Lets admins answer
    # "why did the LLM only update MEMORY.md and not USER.md / SOUL.md?"
    # which the ``*_updated`` boolean flags above cannot. ``prompt_text``
    # is the trimmed conversation passed as the ``<conversation>`` block
    # (the four memory inputs to the prompt are already covered by the
    # ``*_text_before`` snapshots above). ``raw_response_text`` is the
    # unparsed model output, useful when ``_parse_compaction_response``
    # falls back to the empty result. ``parsed_response_json`` is a
    # JSON-serialized ``CompactionResult`` so the four field strings
    # are inspectable without re-parsing the raw response. All three
    # share the migration-031 nullable / envelope-encrypted shape and
    # are subject to the same per-file truncation cap as the 030
    # snapshots.
    prompt_text: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="prompt_text"),
        nullable=True,
    )
    raw_response_text: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="raw_response_text"),
        nullable=True,
    )
    parsed_response_json: Mapped[str | None] = mapped_column(
        EncryptedString(table="compaction_events", column="parsed_response_json"),
        nullable=True,
    )
