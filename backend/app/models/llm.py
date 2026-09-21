"""LLM accounting: usage rows, captured payloads, and named endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base
from backend.app.models.types import EncryptedString

if TYPE_CHECKING:
    # Relationship targets resolve through SQLAlchemy's class registry
    # at mapper-configuration time, so these are annotations only.
    from backend.app.models.user import User


class LLMUsageLog(Base):
    __tablename__ = "llm_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The endpoint that served the call, empty for a bare provider. Kept
    # alongside ``provider`` rather than replacing it: the provider is still
    # the dialect the request was written in, and both are needed to explain
    # a row after the endpoint has been edited or deleted.
    endpoint: Mapped[str] = mapped_column(String, default="")
    provider: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0.000000"))
    # False when ``cost`` is not a cost. Behind a gateway the (provider,
    # model) pair names the dialect rather than whoever billed the tokens, so
    # a price-list hit on it would be a coincidence. Zero is recorded, and
    # this column is what stops a sum reading it as free.
    pricing_available: Mapped[bool] = mapped_column(Boolean, default=True)
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

    user: Mapped[User] = relationship("User", back_populates="llm_usage_logs", lazy="raise")


class LLMPayloadCapture(Base):
    """Bounded rolling capture of LLM request payloads, keyed by user.

    Stores at most two payloads per consenting user: the latest from the
    *previous* compaction era and the latest from the *current* era.
    ``current_era_*`` is always populated when the row exists;
    ``previous_era_*`` stays NULL until the first rotation.

    The era marker is the lowest persisted ``message_seq`` across the
    user/assistant messages in the LLM prompt. When that value rises
    (because compaction trimmed older messages), the capture service
    rotates current into previous before writing the new current.

    ``user_id`` is the sole primary key, which assumes the
    ``UNIQUE(user_id)`` constraint on ``chat_sessions`` holds. Concurrent
    multi-session per user would race two agent loops onto the same row
    with different era markers and ping-pong the rotation; if multi-session
    returns, the PK has to become ``(user_id, session_id)``.
    """

    __tablename__ = "llm_payload_captures"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    current_era_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # NULL when no message in the captured prompt has a persisted seq (e.g.
    # a brand-new conversation). The rotation upsert treats NULL as a
    # distinct value from any integer, so the first capture of a fresh
    # session does not rotate when the next capture has a real seq.
    current_era_min_message_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_era_captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_era_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_era_payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Populated by ``capture_llm_response`` after the matching request
    # lands in ``current_era_payload``. Match keys are ``user_id`` plus the
    # request_id echoed on the response payload. A response that arrives
    # without a matching request_id is dropped (the request was filtered
    # out by purpose, size, or consent).
    current_era_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    current_era_response_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_era_response_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    previous_era_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    previous_era_min_message_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    previous_era_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_era_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    previous_era_payload_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    previous_era_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    previous_era_response_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_era_response_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LLMEndpoint(Base):
    """An operator-named place to send LLM traffic.

    Before this table, a deployment pointed at a gateway by setting
    ``LLM_PROVIDER`` to whichever provider's wire format the gateway spoke
    and ``LLM_API_BASE`` to its URL. That conflated four separate decisions
    into one string. The provider name selects the any-llm adapter, and it
    is also what the code consults to decide whether ``cache_control``
    markers are worth stamping, how to express reasoning, and which
    genai-prices entry describes the cost. A gateway that speaks Anthropic's
    dialect while serving somebody else's model made three of those four
    answers wrong, silently: markers stamped for a hop that drops them, an
    Anthropic thinking budget sent to a model that wants
    ``reasoning_effort``, and a cost column priced as if Anthropic had
    served the tokens.

    An endpoint separates them. ``dialect`` picks the adapter; the
    capability columns state what the far side actually supports instead of
    inferring it from a name that no longer describes the vendor.
    """

    __tablename__ = "llm_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # What a model selection refers to: settings.llm_endpoint, a
    # subscription override, or an eval run's baseline/candidate side.
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The any-llm provider whose wire format this endpoint speaks. Not
    # necessarily the vendor that ends up serving the request.
    dialect: Mapped[str] = mapped_column(String(64), default="")
    base_url: Mapped[str] = mapped_column(String(512), default="")
    # Empty leaves ``api_key`` off the call, which is what lets any-llm fall
    # back to the dialect's own environment variable. Set it when the
    # gateway has its own credential, so pointing at a third party does not
    # hand over the key the dialect's vendor issued.
    api_key: Mapped[str] = mapped_column(
        EncryptedString(table="llm_endpoints", column="api_key"), default=""
    )
    # "auto" defers to the dialect (see llm_service._CACHE_CONTROL_PROVIDERS).
    # "never" is the setting for a gateway that drops the markers or rejects
    # them outright; "always" is for one that forwards them.
    cache_control: Mapped[str] = mapped_column(String(16), default="auto")
    # How to ask for reasoning: "thinking" is the Anthropic budget dict,
    # "effort" the OpenAI-style scalar, "none" omits it. "auto" follows the
    # dialect. See llm_service.ReasoningStyle for why "none" exists.
    reasoning: Mapped[str] = mapped_column(String(16), default="auto")
    # "unpriced" when (dialect, model) does not describe who actually billed
    # the tokens, so a cost total would be fiction. Read by the evaluator.
    pricing: Mapped[str] = mapped_column(String(16), default="auto")
    notes: Mapped[str] = mapped_column(String(256), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
