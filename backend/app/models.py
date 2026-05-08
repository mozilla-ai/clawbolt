"""SQLAlchemy ORM models for clawbolt."""

import logging
import uuid as _uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .config import settings
from .database import Base
from .security import encryption as _encryption

_logger = logging.getLogger(__name__)


class EncryptedString(TypeDecorator):
    """Envelope-encrypts string values at rest.

    On write, generates a fresh per-row data-encryption key (DEK),
    Fernet-encrypts the plaintext with it, and asks the configured
    ``KEKProvider`` to wrap the DEK. The wrapped DEK and the ciphertext
    are serialized into a single inline envelope (see
    ``backend.app.security.encryption``).

    On read, the envelope is parsed, the DEK is unwrapped via the
    provider, and the plaintext is recovered.

    The provider is selected through ``backend.app.auth.loader``: OSS
    ships ``LocalKEKProvider`` (Fernet wrapping derived from
    ``settings.encryption_key``); premium plugins override with a
    KMS-backed provider for per-tenant DEK wrapping.
    """

    impl = Text
    cache_ok = True

    def __init__(self, *, table: str = "", column: str = "") -> None:
        super().__init__()
        self._table = table
        self._column = column

    def _context(self) -> _encryption.EncryptionContext:
        ctx: _encryption.EncryptionContext = {}
        if self._table:
            ctx["table"] = self._table
        if self._column:
            ctx["column"] = self._column
        return ctx

    def _get_provider(self) -> _encryption.KEKProvider:
        # Imported lazily to avoid an import cycle: loader imports models
        # transitively via the auth backend stack.
        from backend.app.auth.loader import get_kek_provider

        return get_kek_provider()

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None or value == "":
            return value
        provider = self._get_provider()
        return _encryption.encrypt(value, provider, self._context())

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None or value == "":
            return value
        if not _encryption.is_envelope(value):
            # Migration 018 re-keys every existing row to the envelope
            # format. A non-envelope value here means the migration
            # didn't run (or a legacy code path bypassed the type
            # decorator). Fail loudly rather than silently return
            # ciphertext or plaintext that would corrupt downstream use.
            raise RuntimeError(
                "EncryptedString read found a non-envelope value. Run "
                "`uv run alembic upgrade head` to re-key existing rows."
            )
        provider = self._get_provider()
        return _encryption.decrypt(value, provider, self._context())


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
    # User research / data sharing consent. Defaults to False so admins
    # only see message bodies, memory, and other user content for users
    # who explicitly opted in here. ``data_sharing_consent_at`` is set
    # on every change (opt-in AND opt-out) so consent history can be
    # reconstructed by joining against an audit log if needed.
    data_sharing_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_sharing_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
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

    channel_routes: Mapped[list["ChannelRoute"]] = relationship(
        "ChannelRoute", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    sessions: Mapped[list["ChatSession"]] = relationship(
        "ChatSession", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    memory_documents: Mapped[list["MemoryDocument"]] = relationship(
        "MemoryDocument", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    heartbeat_logs: Mapped[list["HeartbeatLog"]] = relationship(
        "HeartbeatLog", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    llm_usage_logs: Mapped[list["LLMUsageLog"]] = relationship(
        "LLMUsageLog", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    tool_configs: Mapped[list["ToolConfig"]] = relationship(
        "ToolConfig", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    calendar_configs: Mapped[list["CalendarConfig"]] = relationship(
        "CalendarConfig", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    oauth_tokens: Mapped[list["OAuthToken"]] = relationship(
        "OAuthToken", back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    # Reports this user filed via ``/report``. ``ReportedConversation``
    # has two FKs to ``users.id`` (``user_id`` for the reporter,
    # ``reviewed_admin_user_id`` for the admin who closed it); the
    # explicit ``foreign_keys=`` disambiguates that this relationship
    # is the reporter side. We don't expose the admin-side relationship
    # because the audit log already lets ops query "what did this admin
    # do" without an ORM round-trip.
    reported_conversations: Mapped[list["ReportedConversation"]] = relationship(
        "ReportedConversation",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="ReportedConversation.user_id",
        lazy="raise",
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
    # Set whenever an inbound message resolves to this route. The channel
    # picker UI reads this field to flip to a "Verified" state so users see
    # that their configured channel actually delivers messages end-to-end.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="channel_routes", lazy="raise")


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

    user: Mapped["User"] = relationship("User", back_populates="sessions", lazy="raise")
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan", lazy="raise"
    )


class Message(Base):
    """A single message in a conversation, inbound or outbound.

    User-authored content (``body``, ``processed_context``,
    ``tool_interactions_json``, ``thinking_text``) is envelope-encrypted
    at rest via ``EncryptedString``. ``body`` is the raw text the user /
    channel sent; ``processed_context`` is the same content after media
    transcription / OCR / preprocessing; ``tool_interactions_json``
    holds tool call args / results that frequently embed customer
    names, phone numbers, and addresses passed to QuickBooks /
    CompanyCam / calendar tools. ``thinking_text`` holds the LLM's
    extended-thinking blocks for outbound messages (empty for inbound),
    which can quote user content back at length and so receives the
    same encryption treatment. The decrypt path runs transparently on
    every ORM read, so application code keeps reading
    ``msg.tool_interactions_json`` and gets plaintext JSON.

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
    external_message_id: Mapped[str] = mapped_column(String, default="")
    media_urls_json: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    session: Mapped["ChatSession"] = relationship(
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

    user: Mapped["User"] = relationship("User", back_populates="memory_documents", lazy="raise")


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

    user: Mapped["User"] = relationship("User", back_populates="heartbeat_logs", lazy="raise")


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
    user: Mapped["User"] = relationship(
        "User",
        back_populates="reported_conversations",
        foreign_keys=[user_id],
        lazy="raise",
    )


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
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    cache_read_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", back_populates="llm_usage_logs", lazy="raise")


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

    user: Mapped["User"] = relationship("User", back_populates="calendar_configs", lazy="raise")


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

    user: Mapped["User"] = relationship("User", back_populates="tool_configs", lazy="raise")


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

    user: Mapped["User"] = relationship("User", back_populates="oauth_tokens", lazy="raise")


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


class AppSetting(Base):
    """Runtime-configurable settings keyed by name.

    Backs ``backend.app.config_store.DbSettingsStore``. Reads happen
    once at lifespan boot and on admin updates. Secret values (per the
    store's ``_SECRET_SETTINGS`` allowlist) are envelope-encrypted into
    ``value``; non-secret values are stored verbatim. ``is_secret``
    locks each row to the policy in effect at write time so a future
    allowlist change can't silently misread a row.

    ``updated_at`` and the empty-string default for ``value`` use
    ``server_default`` because the store writes via raw ``INSERT ... ON
    CONFLICT`` rather than the ORM, so Python-side defaults wouldn't
    fire on plain SQL paths.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
