"""Database-backed replacements for file-based stores.

Replaces HeartbeatStore, MediaStore, IdempotencyStore, LLMUsageStore, and
ToolConfigStore from file_store.py. Uses the corresponding ORM models for
persistence, while keeping Pydantic DTOs as the public API surface.

Follows the same SessionLocal() / try-finally pattern used in session_db.py.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from sqlalchemy import func

from backend.app.agent.dto import (
    HeartbeatLogEntry,
    MediaData,
    ToolConfigEntry,
)
from backend.app.database import SessionLocal, db_session
from backend.app.models import (
    HeartbeatLog,
    IdempotencyKey,
    LLMUsageLog,
    MediaFile,
    ToolConfig,
    User,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM -> DTO converters
# ---------------------------------------------------------------------------


def _heartbeat_log_to_dto(log: HeartbeatLog) -> HeartbeatLogEntry:
    return HeartbeatLogEntry(
        user_id=log.user_id,
        action_type=log.action_type or "send",
        message_text=log.message_text or "",
        channel=log.channel or "",
        reasoning=log.reasoning or "",
        tasks=log.tasks or "",
        created_at=log.created_at.isoformat() if log.created_at else "",
    )


def _media_to_dto(m: MediaFile) -> MediaData:
    return MediaData(
        id=m.id,
        message_id=m.message_id,
        user_id=m.user_id,
        original_url=m.original_url,
        mime_type=m.mime_type,
        processed_text=m.processed_text,
        storage_url=m.storage_url,
        storage_path=m.storage_path,
        created_at=m.created_at.isoformat() if m.created_at else "",
    )


def _parse_disabled_sub_tools(raw: str) -> list[str]:
    """Parse JSON list of disabled sub-tool names from DB column."""
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (ValueError, TypeError):
        pass
    return []


def _tool_config_to_dto(tc: ToolConfig) -> ToolConfigEntry:
    return ToolConfigEntry(
        name=tc.name,
        description=tc.description,
        category=tc.category,
        domain_group=tc.domain_group,
        domain_group_order=tc.domain_group_order,
        enabled=tc.enabled,
        disabled_sub_tools=_parse_disabled_sub_tools(tc.disabled_sub_tools),
    )


# ---------------------------------------------------------------------------
# HeartbeatStore
# ---------------------------------------------------------------------------


_MEDIA_UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "processed_text",
        "storage_url",
        "storage_path",
    }
)


class HeartbeatStore:
    """Database-backed heartbeat storage using User.heartbeat_text and HeartbeatLog ORM models."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def read_heartbeat_md(self) -> str:
        """Read freeform heartbeat markdown from User.heartbeat_text."""
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(id=self.user_id).first()
            if user is not None and user.heartbeat_text:
                return user.heartbeat_text
            return ""
        finally:
            db.close()

    async def write_heartbeat_md(self, text: str) -> None:
        """Write freeform heartbeat markdown to User.heartbeat_text."""
        with db_session() as db:
            user = db.query(User).filter_by(id=self.user_id).first()
            if user is not None:
                user.heartbeat_text = text
                db.commit()

    async def log_heartbeat(
        self,
        *,
        action_type: str = "send",
        message_text: str = "",
        channel: str = "",
        reasoning: str = "",
        tasks: str = "",
    ) -> None:
        """Insert a HeartbeatLog row."""
        with db_session() as db:
            log = HeartbeatLog(
                user_id=self.user_id,
                action_type=action_type,
                message_text=message_text,
                channel=channel,
                reasoning=reasoning,
                tasks=tasks,
            )
            db.add(log)
            db.commit()

    async def get_daily_count(self) -> int:
        """Count HeartbeatLog entries for today (UTC), excluding skips."""
        db = SessionLocal()
        try:
            today = datetime.datetime.now(datetime.UTC).date()
            today_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=datetime.UTC)
            tomorrow_start = today_start + datetime.timedelta(days=1)
            count: int = (
                db.query(func.count(HeartbeatLog.id))
                .filter(
                    HeartbeatLog.user_id == self.user_id,
                    HeartbeatLog.created_at >= today_start,
                    HeartbeatLog.created_at < tomorrow_start,
                    HeartbeatLog.action_type != "skip",
                )
                .scalar()
            ) or 0
            return count
        finally:
            db.close()

    async def get_recent_logs(
        self,
        since: datetime.datetime,
    ) -> list[HeartbeatLogEntry]:
        """Select HeartbeatLog entries where created_at >= since."""
        db = SessionLocal()
        try:
            logs = (
                db.query(HeartbeatLog)
                .filter(
                    HeartbeatLog.user_id == self.user_id,
                    HeartbeatLog.created_at >= since,
                )
                .order_by(HeartbeatLog.created_at)
                .all()
            )
            return [_heartbeat_log_to_dto(log) for log in logs]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# MediaStore
# ---------------------------------------------------------------------------


class MediaStore:
    """Database-backed media file storage using MediaFile ORM model."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def list_all(self) -> list[MediaData]:
        """Query all MediaFile rows, return as DTOs."""
        db = SessionLocal()
        try:
            rows = (
                db.query(MediaFile)
                .filter_by(user_id=self.user_id)
                .order_by(MediaFile.created_at)
                .all()
            )
            return [_media_to_dto(m) for m in rows]
        finally:
            db.close()

    async def create(
        self,
        original_url: str = "",
        mime_type: str = "",
        processed_text: str = "",
        storage_url: str = "",
        storage_path: str = "",
        message_id: str | None = None,
    ) -> MediaData:
        """Insert a new MediaFile row and return it as a DTO."""
        with db_session() as db:
            # ID generation: "media-NNN" format -- lock rows to prevent races
            existing_ids = [
                row[0]
                for row in db.query(MediaFile.id)
                .filter_by(user_id=self.user_id)
                .with_for_update()
                .all()
            ]
            max_num = 0
            for mid in existing_ids:
                if mid.startswith("media-"):
                    try:
                        num = int(mid[6:])
                        max_num = max(max_num, num)
                    except ValueError:
                        pass
            new_id = f"media-{max_num + 1:03d}"

            media = MediaFile(
                id=new_id,
                user_id=self.user_id,
                message_id=message_id or "",
                original_url=original_url,
                mime_type=mime_type,
                processed_text=processed_text,
                storage_url=storage_url,
                storage_path=storage_path,
            )
            db.add(media)
            db.commit()
            db.refresh(media)
            return _media_to_dto(media)

    async def update(self, media_id: str, **fields: Any) -> MediaData | None:
        """Update a MediaFile row by id."""
        with db_session() as db:
            m = db.query(MediaFile).filter_by(id=media_id, user_id=self.user_id).first()
            if m is None:
                return None
            for key, value in fields.items():
                if value is not None and key in _MEDIA_UPDATABLE_FIELDS:
                    setattr(m, key, value)
            db.commit()
            db.refresh(m)
            return _media_to_dto(m)

    async def get_by_url(self, original_url: str) -> MediaData | None:
        """Query a MediaFile by original_url."""
        db = SessionLocal()
        try:
            m = (
                db.query(MediaFile)
                .filter_by(user_id=self.user_id, original_url=original_url)
                .first()
            )
            return _media_to_dto(m) if m else None
        finally:
            db.close()

    async def count_by_path_prefix(self, prefix: str) -> int:
        """Count MediaFile rows where storage_path starts with prefix."""
        db = SessionLocal()
        try:
            count: int = (
                db.query(func.count(MediaFile.id))
                .filter(
                    MediaFile.user_id == self.user_id,
                    MediaFile.storage_path.startswith(prefix),
                )
                .scalar()
            ) or 0
            return count
        finally:
            db.close()


# ---------------------------------------------------------------------------
# IdempotencyStore
# ---------------------------------------------------------------------------

_SEEN_MAX = 10_000


class IdempotencyStore:
    """Database-backed idempotency tracking for webhook deduplication.

    Uses the IdempotencyKey ORM model. No user_id scoping -- external_id
    is globally unique.
    """

    def has_seen(self, external_id: str) -> bool:
        """Check if an external message ID has been seen."""
        db = SessionLocal()
        try:
            row = db.query(IdempotencyKey).filter_by(external_id=external_id).first()
            return row is not None
        finally:
            db.close()

    async def mark_seen(self, external_id: str) -> None:
        """Insert an IdempotencyKey row (ignore if it already exists)."""
        from sqlalchemy.exc import IntegrityError

        with db_session() as db:
            existing = db.query(IdempotencyKey).filter_by(external_id=external_id).first()
            if existing is not None:
                return
            key = IdempotencyKey(external_id=external_id)
            db.add(key)
            try:
                db.commit()
            except IntegrityError:
                # Concurrent insert won the race; the key is already marked
                db.rollback()


# ---------------------------------------------------------------------------
# LLMUsageStore
# ---------------------------------------------------------------------------


class LLMUsageStore:
    """Database-backed LLM usage logging using LLMUsageLog ORM model."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def log(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        purpose: str,
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
    ) -> None:
        """Insert a LLMUsageLog row.

        Maps prompt_tokens -> input_tokens, completion_tokens -> output_tokens
        as the ORM model uses input_tokens/output_tokens naming.  Sets
        provider="" and cost=0.0 since the file store did not track those.
        """
        with db_session() as db:
            entry = LLMUsageLog(
                user_id=self.user_id,
                provider="",
                model=model,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cost=0.0,
                purpose=purpose,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
            )
            db.add(entry)
            db.commit()


# ---------------------------------------------------------------------------
# ToolConfigStore
# ---------------------------------------------------------------------------


class ToolConfigStore:
    """Database-backed tool configuration using ToolConfig ORM model."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def load(self) -> list[ToolConfigEntry]:
        """Query all ToolConfig rows for this user, return as DTOs."""
        db = SessionLocal()
        try:
            rows = (
                db.query(ToolConfig)
                .filter_by(user_id=self.user_id)
                .order_by(ToolConfig.domain_group_order, ToolConfig.name)
                .all()
            )
            return [_tool_config_to_dto(tc) for tc in rows]
        finally:
            db.close()

    async def save(self, entries: list[ToolConfigEntry]) -> list[ToolConfigEntry]:
        """Replace all ToolConfig rows for this user with new entries."""
        with db_session() as db:
            # Delete existing rows for this user
            db.query(ToolConfig).filter_by(user_id=self.user_id).delete()

            # Insert new rows
            for entry in entries:
                disabled_sub = (
                    json.dumps(entry.disabled_sub_tools) if entry.disabled_sub_tools else ""
                )
                tc = ToolConfig(
                    user_id=self.user_id,
                    name=entry.name,
                    description=entry.description,
                    category=entry.category,
                    domain_group=entry.domain_group,
                    domain_group_order=entry.domain_group_order,
                    enabled=entry.enabled,
                    disabled_sub_tools=disabled_sub,
                )
                db.add(tc)
            db.commit()
            return entries

    async def get_disabled_tool_names(self) -> set[str]:
        """Return the set of tool group names that are disabled."""
        db = SessionLocal()
        try:
            rows = db.query(ToolConfig.name).filter_by(user_id=self.user_id, enabled=False).all()
            return {row[0] for row in rows}
        finally:
            db.close()

    async def get_disabled_sub_tool_names(self) -> set[str]:
        """Return the union of all disabled sub-tool names across all groups."""
        db = SessionLocal()
        try:
            rows = (
                db.query(ToolConfig.disabled_sub_tools)
                .filter_by(user_id=self.user_id)
                .filter(ToolConfig.disabled_sub_tools != "")
                .all()
            )
            result: set[str] = set()
            for (raw,) in rows:
                result.update(_parse_disabled_sub_tools(raw))
            return result
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Module-level singletons / factories
# ---------------------------------------------------------------------------

_idempotency_store: IdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore:
    global _idempotency_store
    if _idempotency_store is None:
        _idempotency_store = IdempotencyStore()
    return _idempotency_store


def reset_stores() -> None:
    """Reset cached store instances. Used by tests."""
    global _idempotency_store
    _idempotency_store = None

    from backend.app.agent.user_db import reset_user_store

    reset_user_store()
