"""Database-backed replacements for file-based stores.

Replaces HeartbeatStore, MediaStore, IdempotencyStore, LLMUsageStore, and
ToolConfigStore from file_store.py. Uses the corresponding ORM models for
persistence, while keeping Pydantic DTOs as the public API surface.

Follows the same AsyncSessionLocal() / try-finally pattern used in session_db.py.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import Delete, Select, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.dto import (
    HeartbeatLogEntry,
    MediaData,
    ToolConfigEntry,
)
from backend.app.database import AsyncSessionLocal, db_session_async
from backend.app.models import (
    HeartbeatLog,
    IdempotencyKey,
    LLMUsageLog,
    MediaFile,
    ToolConfig,
    User,
)
from backend.app.services.llm_pricing import compute_cost, is_known_model

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


# Builders shared by sync and async heartbeat methods (issue #1154).
# Same dual-API pattern as the IdempotencyStore pilot in #1199: pure
# ``select(...)`` builders so the two paths stay in lockstep without a
# class hierarchy.
def _heartbeat_user_select(user_id: str) -> Select[tuple[User]]:
    """Builder shared by ``read_heartbeat_md`` / ``write_heartbeat_md`` peers."""
    return select(User).filter_by(id=user_id)


def _heartbeat_daily_count_select(
    user_id: str,
    today_start: datetime.datetime,
    tomorrow_start: datetime.datetime,
) -> Select[tuple[int]]:
    """Builder shared by ``get_daily_count`` / ``get_daily_count_async``."""
    return select(func.count(HeartbeatLog.id)).where(
        HeartbeatLog.user_id == user_id,
        HeartbeatLog.created_at >= today_start,
        HeartbeatLog.created_at < tomorrow_start,
        HeartbeatLog.action_type.notin_(("skip", "cleanup")),
    )


def _recent_heartbeat_logs_select(
    user_id: str, since: datetime.datetime
) -> Select[tuple[HeartbeatLog]]:
    """Builder shared by ``get_recent_logs`` / ``get_recent_logs_async``."""
    return (
        select(HeartbeatLog)
        .where(
            HeartbeatLog.user_id == user_id,
            HeartbeatLog.created_at >= since,
        )
        .order_by(HeartbeatLog.created_at)
    )


def _today_window_utc() -> tuple[datetime.datetime, datetime.datetime]:
    """Compute today's [start, end) window in UTC for daily heartbeat counts."""
    today = datetime.datetime.now(datetime.UTC).date()
    today_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=datetime.UTC)
    tomorrow_start = today_start + datetime.timedelta(days=1)
    return today_start, tomorrow_start


class HeartbeatStore:
    """Database-backed heartbeat storage using User.heartbeat_text and HeartbeatLog ORM models.

    Async-only API after the issue #1160 final pass. The sync
    ``read_heartbeat_md`` method introduced as a transition shim in
    issue #1154 has been removed; all OSS and premium callers use
    :meth:`read_heartbeat_md_async`.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def read_heartbeat_md_async(self) -> str:
        """Read freeform heartbeat markdown from User.heartbeat_text."""
        db = AsyncSessionLocal()
        try:
            user = (await db.execute(_heartbeat_user_select(self.user_id))).scalar_one_or_none()
            if user is not None and user.heartbeat_text:
                return user.heartbeat_text
            return ""
        finally:
            await db.close()

    async def write_heartbeat_md(self, text: str) -> None:
        """Write freeform heartbeat markdown to User.heartbeat_text."""
        async with db_session_async() as db:
            user = (await db.execute(_heartbeat_user_select(self.user_id))).scalar_one_or_none()
            if user is not None:
                user.heartbeat_text = text
                await db.commit()

    async def write_heartbeat_md_async(self, text: str) -> None:
        """Deprecated alias of :meth:`write_heartbeat_md`."""
        await self.write_heartbeat_md(text)

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
        async with db_session_async() as db:
            log = HeartbeatLog(
                user_id=self.user_id,
                action_type=action_type,
                message_text=message_text,
                channel=channel,
                reasoning=reasoning,
                tasks=tasks,
            )
            db.add(log)
            await db.commit()

    async def log_heartbeat_async(
        self,
        *,
        action_type: str = "send",
        message_text: str = "",
        channel: str = "",
        reasoning: str = "",
        tasks: str = "",
    ) -> None:
        """Deprecated alias of :meth:`log_heartbeat`."""
        await self.log_heartbeat(
            action_type=action_type,
            message_text=message_text,
            channel=channel,
            reasoning=reasoning,
            tasks=tasks,
        )

    async def get_daily_count(self) -> int:
        """Count HeartbeatLog entries for today (UTC) that consumed the nudge budget.

        Excludes ``"skip"`` (Phase 1 chose no action) and ``"cleanup"``
        (Phase 2 ran but produced no user-facing message, e.g. pruning
        a stale HEARTBEAT.md entry). Both are audit/dedup signals, not
        nudges to the user, so they do not count toward
        ``heartbeat_max_daily_messages``.
        """
        db = AsyncSessionLocal()
        try:
            today_start, tomorrow_start = _today_window_utc()
            count: int = (
                await db.scalar(
                    _heartbeat_daily_count_select(self.user_id, today_start, tomorrow_start)
                )
            ) or 0
            return count
        finally:
            await db.close()

    async def get_daily_count_async(self) -> int:
        """Deprecated alias of :meth:`get_daily_count`."""
        return await self.get_daily_count()

    async def get_recent_logs(
        self,
        since: datetime.datetime,
    ) -> list[HeartbeatLogEntry]:
        """Select HeartbeatLog entries where created_at >= since."""
        db = AsyncSessionLocal()
        try:
            result = await db.execute(_recent_heartbeat_logs_select(self.user_id, since))
            logs = result.scalars().all()
            return [_heartbeat_log_to_dto(log) for log in logs]
        finally:
            await db.close()

    async def get_recent_logs_async(
        self,
        since: datetime.datetime,
    ) -> list[HeartbeatLogEntry]:
        """Deprecated alias of :meth:`get_recent_logs`."""
        return await self.get_recent_logs(since)


# ---------------------------------------------------------------------------
# MediaStore
# ---------------------------------------------------------------------------


# Builders shared by sync and async media methods (issue #1155). Pure
# ``select(...)`` builders so the two paths stay in lockstep without a
# class hierarchy. Same pattern as the IdempotencyStore pilot.
def _media_list_select(user_id: str) -> Select[tuple[MediaFile]]:
    """Builder shared by ``list_all`` / ``list_all_async``."""
    return select(MediaFile).filter_by(user_id=user_id).order_by(MediaFile.created_at)


def _media_existing_ids_select(user_id: str) -> Select[tuple[str]]:
    """Builder shared by the ``create`` / ``create_async`` ID-allocation paths.

    Locks the existing rows ``FOR UPDATE`` so two concurrent inserts do
    not race to allocate the same ``media-NNN`` id.
    """
    return select(MediaFile.id).filter_by(user_id=user_id).with_for_update()


def _media_by_id_select(user_id: str, media_id: str) -> Select[tuple[MediaFile]]:
    """Builder shared by the ``update`` / ``update_async`` paths."""
    return select(MediaFile).filter_by(id=media_id, user_id=user_id)


def _media_by_url_select(user_id: str, original_url: str) -> Select[tuple[MediaFile]]:
    """Builder shared by ``get_by_url`` / ``get_by_url_async``."""
    return select(MediaFile).where(
        MediaFile.user_id == user_id,
        or_(
            MediaFile.original_url == original_url,
            MediaFile.storage_url == original_url,
            MediaFile.storage_path == original_url,
        ),
    )


def _media_count_by_prefix_select(user_id: str, prefix: str) -> Select[tuple[int]]:
    """Builder shared by ``count_by_path_prefix`` / ``count_by_path_prefix_async``."""
    return select(func.count(MediaFile.id)).where(
        MediaFile.user_id == user_id,
        MediaFile.storage_path.startswith(prefix),
    )


def _media_recent_select(user_id: str, limit: int) -> Select[tuple[MediaFile]]:
    """Builder shared by read-side recent-media lookups."""
    return (
        select(MediaFile)
        .where(MediaFile.user_id == user_id)
        .order_by(MediaFile.created_at.desc())
        .limit(limit)
    )


def _media_search_tokens(query: str) -> list[str]:
    """Tokenize a search string for case-insensitive saved-file lookup."""
    return [token for token in re.split(r"\W+", query.lower()) if token]


def _media_search_select(user_id: str, query: str, limit: int) -> Select[tuple[MediaFile]]:
    """Builder shared by read-side saved-file search."""
    tokens = _media_search_tokens(query)
    clauses: list[Any] = [MediaFile.user_id == user_id]
    for token in tokens:
        needle = f"%{token}%"
        clauses.append(
            or_(
                func.lower(MediaFile.storage_path).like(needle),
                func.lower(MediaFile.processed_text).like(needle),
                func.lower(MediaFile.mime_type).like(needle),
            )
        )
    return select(MediaFile).where(*clauses).order_by(MediaFile.created_at.desc()).limit(limit)


def _next_media_id(existing_ids: list[str]) -> str:
    """Compute the next ``media-NNN`` id from the locked existing-id set.

    Pure helper so ``create`` / ``create_async`` use identical
    allocation semantics. The caller owns the lock and the surrounding
    transaction.
    """
    max_num = 0
    for mid in existing_ids:
        if mid.startswith("media-"):
            try:
                num = int(mid[6:])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return f"media-{max_num + 1:03d}"


def _apply_media_updates(m: MediaFile, fields: dict[str, Any]) -> None:
    """Apply allowlisted attribute updates to a MediaFile row in place.

    Pure helper so ``update`` / ``update_async`` use identical
    field-allowlist semantics. ``None`` values are skipped to match the
    existing partial-update contract.
    """
    for key, value in fields.items():
        if value is not None and key in _MEDIA_UPDATABLE_FIELDS:
            setattr(m, key, value)


class MediaStore:
    """Database-backed media file storage using MediaFile ORM model.

    Async-only API (issue #1160). The dual sync+async surface from
    issue #1155 has been collapsed: only the async implementation
    remains. ``*_async`` aliases stay as thin wrappers so premium
    continues to compile while it drops the suffix on its own callers.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def list_all(self) -> list[MediaData]:
        """Query all MediaFile rows, return as DTOs."""
        db = AsyncSessionLocal()
        try:
            result = await db.execute(_media_list_select(self.user_id))
            rows = result.scalars().all()
            return [_media_to_dto(m) for m in rows]
        finally:
            await db.close()

    async def list_all_async(self) -> list[MediaData]:
        """Deprecated alias of :meth:`list_all`."""
        return await self.list_all()

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
        async with db_session_async() as db:
            result = await db.execute(_media_existing_ids_select(self.user_id))
            existing_ids = list(result.scalars().all())
            new_id = _next_media_id(existing_ids)

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
            await db.commit()
            await db.refresh(media)
            return _media_to_dto(media)

    async def create_async(
        self,
        original_url: str = "",
        mime_type: str = "",
        processed_text: str = "",
        storage_url: str = "",
        storage_path: str = "",
        message_id: str | None = None,
    ) -> MediaData:
        """Deprecated alias of :meth:`create`."""
        return await self.create(
            original_url=original_url,
            mime_type=mime_type,
            processed_text=processed_text,
            storage_url=storage_url,
            storage_path=storage_path,
            message_id=message_id,
        )

    async def update(self, media_id: str, **fields: Any) -> MediaData | None:
        """Update a MediaFile row by id."""
        async with db_session_async() as db:
            m = (await db.execute(_media_by_id_select(self.user_id, media_id))).scalar_one_or_none()
            if m is None:
                return None
            _apply_media_updates(m, fields)
            await db.commit()
            await db.refresh(m)
            return _media_to_dto(m)

    async def update_async(self, media_id: str, **fields: Any) -> MediaData | None:
        """Deprecated alias of :meth:`update`."""
        return await self.update(media_id, **fields)

    async def get_by_url(self, original_url: str) -> MediaData | None:
        """Query a MediaFile by any of its stored URLs or paths.

        Matches ``original_url`` (channel attachment id, e.g. ``bb_<guid>``),
        ``storage_url`` (backend-emitted URL shown to the LLM in upload
        results, e.g. ``file:///...``), or ``storage_path`` (the folder path).
        The agent only sees ``storage_url`` in upload_to_storage's result and
        will pass that back to organize_file later; matching on all three
        lets the lookup succeed regardless of which identifier the LLM used.
        """
        if not original_url:
            return None
        db = AsyncSessionLocal()
        try:
            m = (
                await db.execute(_media_by_url_select(self.user_id, original_url))
            ).scalar_one_or_none()
            return _media_to_dto(m) if m else None
        finally:
            await db.close()

    async def get_by_url_async(self, original_url: str) -> MediaData | None:
        """Deprecated alias of :meth:`get_by_url`."""
        return await self.get_by_url(original_url)

    async def get_by_id(self, media_id: str) -> MediaData | None:
        """Query a MediaFile by its durable ``media-NNN`` id."""
        if not media_id:
            return None
        db = AsyncSessionLocal()
        try:
            m = (await db.execute(_media_by_id_select(self.user_id, media_id))).scalar_one_or_none()
            return _media_to_dto(m) if m else None
        finally:
            await db.close()

    async def get_by_id_async(self, media_id: str) -> MediaData | None:
        """Deprecated alias of :meth:`get_by_id`."""
        return await self.get_by_id(media_id)

    async def count_by_path_prefix(self, prefix: str) -> int:
        """Count MediaFile rows where storage_path starts with prefix."""
        db = AsyncSessionLocal()
        try:
            count: int = (await db.scalar(_media_count_by_prefix_select(self.user_id, prefix))) or 0
            return count
        finally:
            await db.close()

    async def count_by_path_prefix_async(self, prefix: str) -> int:
        """Deprecated alias of :meth:`count_by_path_prefix`."""
        return await self.count_by_path_prefix(prefix)

    async def search(self, query: str = "", limit: int = 5) -> list[MediaData]:
        """Search saved files by path/description tokens, or list recent when blank."""
        bounded_limit = max(1, min(limit, 20))
        stmt = (
            _media_recent_select(self.user_id, bounded_limit)
            if not query.strip() or not _media_search_tokens(query)
            else _media_search_select(self.user_id, query, bounded_limit)
        )
        db = AsyncSessionLocal()
        try:
            rows = (await db.execute(stmt)).scalars().all()
            return [_media_to_dto(m) for m in rows]
        finally:
            await db.close()

    async def search_async(self, query: str = "", limit: int = 5) -> list[MediaData]:
        """Deprecated alias of :meth:`search`."""
        return await self.search(query=query, limit=limit)


# ---------------------------------------------------------------------------
# IdempotencyStore
# ---------------------------------------------------------------------------

_SEEN_MAX = 10_000


# Pilot for the per-store dual-API rollout (issue #1150). Internal
# logic is factored into pure ``select(...) / delete(...)`` builders
# so the sync and async methods stay in lockstep without a class
# hierarchy. Each public sync method has an ``*_async`` peer; both
# forward through the same builders. Stores #1151-#1157 should
# follow this pattern.
def _seen_select(external_id: str) -> Select[tuple[IdempotencyKey]]:
    """Builder shared by ``has_seen`` and ``has_seen_async``."""
    return select(IdempotencyKey).filter_by(external_id=external_id)


def _count_select() -> Select[tuple[int]]:
    """Builder shared by the sync and async ``_prune`` paths."""
    return select(func.count(IdempotencyKey.id))


def _prune_delete() -> Delete[tuple[IdempotencyKey]]:
    """Build the DELETE that keeps the newest ``_SEEN_MAX`` rows.

    Uses a single DELETE with a NOT-IN subquery so the whole thing
    runs in one snapshot under READ COMMITTED; concurrent prunes
    just delete the same set of rows instead of cascading.
    """
    keep = select(IdempotencyKey.id).order_by(IdempotencyKey.id.desc()).limit(_SEEN_MAX)
    return (
        delete(IdempotencyKey)
        .where(IdempotencyKey.id.notin_(keep))
        .execution_options(synchronize_session=False)
    )


class IdempotencyStore:
    """Database-backed idempotency tracking for webhook deduplication.

    Uses the IdempotencyKey ORM model. No user_id scoping -- external_id
    is globally unique.

    Async-only as of issue #1160. The webhook entry path is async, the
    test fixtures drive it through ``httpx.AsyncClient`` so the route
    runs on the same event loop as the test's async DB connection.
    ``*_async`` aliases are kept as thin wrappers for any out-of-tree
    caller still on the suffix.
    """

    async def has_seen(self, external_id: str) -> bool:
        """Check if an external message ID has been seen."""
        db = AsyncSessionLocal()
        try:
            row = (await db.execute(_seen_select(external_id))).scalar_one_or_none()
            return row is not None
        finally:
            await db.close()

    async def has_seen_async(self, external_id: str) -> bool:
        """Deprecated alias of :meth:`has_seen`."""
        return await self.has_seen(external_id)

    async def try_mark_seen(self, external_id: str) -> bool:
        """Atomically insert an IdempotencyKey row and return whether it was new.

        Returns ``True`` if the row was newly inserted (first time seen),
        ``False`` if it already existed (duplicate). The unique-constraint
        violation on ``external_id`` is the source of truth for "already
        seen". A prune failure is logged and swallowed so a transient
        prune error never makes a duplicate webhook re-fire.
        """
        async with db_session_async() as db:
            key = IdempotencyKey(external_id=external_id)
            db.add(key)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return False

            try:
                await self._prune(db)
            except Exception:
                await db.rollback()
                logger.warning("IdempotencyStore prune failed", exc_info=True)
        return True

    async def try_mark_seen_async(self, external_id: str) -> bool:
        """Deprecated alias of :meth:`try_mark_seen`."""
        return await self.try_mark_seen(external_id)

    async def _prune(self, db: AsyncSession | None = None) -> None:
        """Remove the oldest rows when the table exceeds ``_SEEN_MAX``.

        When *db* is provided the caller's session is reused, avoiding
        an extra connection checkout.  When called without a session a
        fresh session is created automatically.

        Keeps the newest rows by ``id`` (autoincrement, so allocation
        order is monotonic even if commit order isn't -- fine for dedup).
        We order by ``id`` rather than ``created_at`` because
        app-generated timestamps can drift across workers.
        """
        if db is None:
            async with db_session_async() as fresh:
                await self._prune(fresh)
            return
        count = (await db.scalar(_count_select())) or 0
        if count <= _SEEN_MAX:
            return
        await db.execute(_prune_delete())
        await db.commit()

    async def mark_seen(self, external_id: str) -> None:
        """Insert an IdempotencyKey row (ignore if it already exists).

        Prefer :meth:`try_mark_seen` for atomic check-and-insert.
        """
        await self.try_mark_seen(external_id)

    async def mark_seen_async(self, external_id: str) -> None:
        """Deprecated alias of :meth:`mark_seen`."""
        await self.try_mark_seen(external_id)


# ---------------------------------------------------------------------------
# LLMUsageStore
# ---------------------------------------------------------------------------


# (provider, model) tuples we have already warned about lacking pricing
# data. Bounded in practice by the number of distinct (provider, model)
# pairs the deployment emits, which is typically 1-3. Avoids one
# warning per LLM call when a new model lands ahead of a genai-prices
# data refresh.
_warned_unpriced_models: set[tuple[str, str]] = set()


def _build_llm_usage_log(
    *,
    user_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    purpose: str,
    provider: str,
    cache_creation_input_tokens: int | None,
    cache_read_input_tokens: int | None,
) -> LLMUsageLog:
    """Compute cost, emit the unpriced-model warning, and build an LLMUsageLog row.

    Pure helper shared by ``LLMUsageStore.log`` and ``log_async`` so the
    two paths use identical pricing semantics, identical warning
    suppression, and identical column population. Does not touch the
    database; the caller owns the session and the surrounding commit.
    """
    cost = compute_cost(
        model,
        provider=provider,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    if (
        not is_known_model(model, provider=provider)
        and (prompt_tokens or completion_tokens)
        and (provider, model) not in _warned_unpriced_models
    ):
        _warned_unpriced_models.add((provider, model))
        logger.warning(
            "genai-prices does not know provider=%r model=%r; logging "
            "usage with cost=0. Bump the genai-prices dependency to "
            "pick up new model pricing.",
            provider,
            model,
        )

    return LLMUsageLog(
        user_id=user_id,
        provider=provider,
        model=model,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
        purpose=purpose,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


class LLMUsageStore:
    """Database-backed LLM usage logging using LLMUsageLog ORM model.

    Async-only API after the issue #1160 final pass. The sync ``log``
    method has been removed; ``services.llm_usage.log_llm_usage`` is
    the canonical async entry point and threads cost computation
    plus the unpriced-model warning through ``_build_llm_usage_log``.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def log_async(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        purpose: str,
        provider: str = "",
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
    ) -> None:
        """Insert a LLMUsageLog row with computed cost.

        Maps prompt_tokens -> input_tokens, completion_tokens -> output_tokens
        as the ORM model uses input_tokens/output_tokens naming. *provider*
        is the any-llm provider id (``"anthropic"``, ``"openai"``, etc.) and
        is persisted alongside the model so downstream analytics never has
        to re-derive it from the model string. Cost is computed via
        ``services.llm_pricing`` (a thin wrapper around the ``genai-prices``
        library); unknown (provider, model) combinations fall through with
        ``cost=0.000000`` and a once-per-process warning so we notice when
        our pricing data is stale.
        """
        entry = _build_llm_usage_log(
            user_id=self.user_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            purpose=purpose,
            provider=provider,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
        async with db_session_async() as db:
            db.add(entry)
            await db.commit()


# ---------------------------------------------------------------------------
# ToolConfigStore
# ---------------------------------------------------------------------------


# Builders shared by sync and async tool-config methods (issue #1157).
# Pure ``select(...) / delete(...)`` builders so the two paths stay in
# lockstep without a class hierarchy. Same pattern as the
# IdempotencyStore pilot.
def _tool_config_load_select(user_id: str) -> Select[tuple[ToolConfig]]:
    """Builder shared by ``load`` / ``load_async``."""
    return (
        select(ToolConfig)
        .filter_by(user_id=user_id)
        .order_by(ToolConfig.domain_group_order, ToolConfig.name)
    )


def _tool_config_delete_for_user(user_id: str) -> Delete[tuple[ToolConfig]]:
    """Builder shared by the ``save`` / ``save_async`` replace-all paths."""
    return delete(ToolConfig).where(ToolConfig.user_id == user_id)


def _tool_config_disabled_names_select(user_id: str) -> Select[tuple[str]]:
    """Builder shared by ``get_disabled_tool_names`` / ``_async``."""
    return select(ToolConfig.name).filter_by(user_id=user_id, enabled=False)


def _tool_config_by_name_select(user_id: str, name: str) -> Select[tuple[ToolConfig]]:
    """Builder shared by the ``set_enabled`` / ``set_enabled_async`` paths."""
    return select(ToolConfig).filter_by(user_id=user_id, name=name)


def _tool_config_disabled_sub_tools_select(user_id: str) -> Select[tuple[str]]:
    """Builder shared by ``get_disabled_sub_tool_names`` / ``_async``."""
    return (
        select(ToolConfig.disabled_sub_tools)
        .filter_by(user_id=user_id)
        .where(ToolConfig.disabled_sub_tools != "")
    )


def _build_tool_config(user_id: str, entry: ToolConfigEntry) -> ToolConfig:
    """Construct a ToolConfig ORM row from a DTO. Pure helper shared by save paths."""
    disabled_sub = json.dumps(entry.disabled_sub_tools) if entry.disabled_sub_tools else ""
    return ToolConfig(
        user_id=user_id,
        name=entry.name,
        description=entry.description,
        category=entry.category,
        domain_group=entry.domain_group,
        domain_group_order=entry.domain_group_order,
        enabled=entry.enabled,
        disabled_sub_tools=disabled_sub,
    )


def _new_disabled_tool_config(user_id: str, name: str, enabled: bool) -> ToolConfig:
    """Construct a placeholder ToolConfig row for ``set_enabled`` when none exists."""
    return ToolConfig(
        user_id=user_id,
        name=name,
        description="",
        category="domain",
        domain_group="",
        domain_group_order=0,
        enabled=enabled,
        disabled_sub_tools="",
    )


class ToolConfigStore:
    """Database-backed tool configuration using ToolConfig ORM model.

    Async-only API (issue #1160). The dual sync+async surface from
    issue #1157 has been collapsed: only the async implementation
    remains. ``*_async`` aliases stay as thin wrappers in case any
    out-of-tree caller still depends on the suffix; the OSS callers
    have all been migrated to the bare names.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def load(self) -> list[ToolConfigEntry]:
        """Query all ToolConfig rows for this user, return as DTOs."""
        db = AsyncSessionLocal()
        try:
            result = await db.execute(_tool_config_load_select(self.user_id))
            rows = result.scalars().all()
            return [_tool_config_to_dto(tc) for tc in rows]
        finally:
            await db.close()

    async def load_async(self) -> list[ToolConfigEntry]:
        """Deprecated alias of :meth:`load`."""
        return await self.load()

    async def save(self, entries: list[ToolConfigEntry]) -> list[ToolConfigEntry]:
        """Replace all ToolConfig rows for this user with new entries."""
        async with db_session_async() as db:
            await db.execute(_tool_config_delete_for_user(self.user_id))

            for entry in entries:
                db.add(_build_tool_config(self.user_id, entry))
            await db.commit()
            return entries

    async def save_async(self, entries: list[ToolConfigEntry]) -> list[ToolConfigEntry]:
        """Deprecated alias of :meth:`save`."""
        return await self.save(entries)

    async def get_disabled_tool_names(self) -> set[str]:
        """Return the set of tool group names that are disabled."""
        db = AsyncSessionLocal()
        try:
            result = await db.execute(_tool_config_disabled_names_select(self.user_id))
            return {row[0] for row in result.all()}
        finally:
            await db.close()

    async def get_disabled_tool_names_async(self) -> set[str]:
        """Deprecated alias of :meth:`get_disabled_tool_names`."""
        return await self.get_disabled_tool_names()

    async def set_enabled(self, name: str, enabled: bool) -> None:
        """Set a single tool group's enabled state.

        Creates or updates a ToolConfig row for the given factory name.
        Only stores the name and enabled flag; the router fills in display
        metadata from the registry when building the full tool list.
        """
        async with db_session_async() as db:
            existing = (
                await db.execute(_tool_config_by_name_select(self.user_id, name))
            ).scalar_one_or_none()
            if existing:
                existing.enabled = enabled
            else:
                db.add(_new_disabled_tool_config(self.user_id, name, enabled))
            await db.commit()

    async def set_enabled_async(self, name: str, enabled: bool) -> None:
        """Deprecated alias of :meth:`set_enabled`."""
        await self.set_enabled(name, enabled)

    async def get_disabled_sub_tool_names(self) -> set[str]:
        """Return the union of all disabled sub-tool names across all groups."""
        db = AsyncSessionLocal()
        try:
            db_result = await db.execute(_tool_config_disabled_sub_tools_select(self.user_id))
            result: set[str] = set()
            for (raw,) in db_result.all():
                result.update(_parse_disabled_sub_tools(raw))
            return result
        finally:
            await db.close()

    async def get_disabled_sub_tool_names_async(self) -> set[str]:
        """Deprecated alias of :meth:`get_disabled_sub_tool_names`."""
        return await self.get_disabled_sub_tool_names()


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
