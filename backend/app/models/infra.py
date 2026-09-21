"""Plumbing tables: webhook dedup, staged media, and app settings."""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class StagedMedia(Base):
    """Inbound media bytes the user sent over a messaging channel.

    Bytes themselves live on the deployment's persistent volume under
    ``settings.media_staging_base_dir`` (see ``disk_path``); this row is
    the metadata that lets the agent find them again across process
    restarts. Rows past ``expires_at`` are deleted by a lazy purge on the
    next write and a startup sweep. See
    ``backend.app.agent.media_staging`` for the public API.

    The pair ``(user_id, original_url)`` is unique: re-staging the same
    URL for a user reuses the existing handle so the agent can reference
    a photo consistently across turns. ``handle`` is globally unique so
    a reverse lookup is one SELECT, not a per-user scan.
    """

    __tablename__ = "staged_media"
    __table_args__ = (
        UniqueConstraint("handle", name="uq_staged_media_handle"),
        UniqueConstraint("user_id", "original_url", name="uq_staged_media_user_original_url"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(_uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    handle: Mapped[str] = mapped_column(String, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String, nullable=False, default="application/octet-stream"
    )
    disk_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Upload receipt: where this handle's bytes were shipped, populated by
    # ``media_staging.mark_uploaded`` after a successful tool call. Lives on
    # the same row as the staged bytes so it shares their TTL and survives
    # worker restarts. ``service`` is None until the bytes are uploaded.
    upload_service: Mapped[str | None] = mapped_column(String, nullable=True)
    upload_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_status: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
