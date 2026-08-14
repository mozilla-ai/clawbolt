"""Capture LLM request payloads for consenting users.

Implements the observer that the agent dispatches to via the
``set_llm_request_observer`` hook. The observer:

1. Filters to ``purpose == PURPOSE_AGENT_MAIN`` -- the post-trim
   follow-up, compaction, and heartbeat dispatches are not captured
   here. Mixing them into the rotation would be wrong because they
   carry no meaningful era marker (or a fake one), so they would
   ping-pong against agent-main captures.
2. Returns immediately and fires the actual work onto an ``asyncio``
   task: the agent loop is on the user-facing latency hot path and
   must not wait for a DB round-trip + JSONB upsert per LLM call.
3. Inside the background task: skips users without
   ``data_sharing_consent``, drops payloads that exceed a hard byte
   cap (``MAX_CAPTURE_BYTES``, sized off the OSS trim ceiling so a
   normal heavy user is captured and only a genuine runaway is
   dropped), then persists exactly two payload slots
   per user (current / previous era) via a single ``INSERT ... ON
   CONFLICT`` upsert that rotates atomically. The era marker is the
   ``min_message_seq_in_prompt`` field on the payload; when it
   changes between captures, the existing current row is rotated
   into the previous slot before the new payload overwrites current.

Single-session assumption: this design relies on OSS migration 026
("Collapse the sessions table to one row per user"), which enforces
``UNIQUE(user_id)`` on ``chat_sessions``. With multiple concurrent
sessions per user, two parallel agent loops would compete for the same
``llm_payload_captures`` row with potentially different era markers,
ping-ponging the rotation and silently destroying the "previous era"
semantic. The PK on ``llm_payload_captures.user_id`` is sized for the
one-session-per-user world; if OSS ever reintroduces multi-session, the
PK must become ``(user_id, session_id)`` and the migration must
backfill.

The observer never raises -- OSS catches exceptions, and the background
task body is wrapped in try/except so a transient DB error logs and
moves on without crashing the worker task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import case as sa_case
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.observer import (
    PURPOSE_AGENT_MAIN,
    LLMRequestPayload,
    LLMResponsePayload,
    set_llm_request_observer,
    set_llm_response_observer,
)
from backend.app.models import User

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

from backend.app.database import db_session_async
from backend.app.models import LLMPayloadCapture

logger = logging.getLogger(__name__)

# Hard cap on a single capture's serialized JSON size. Captures larger than
# this are dropped.
#
# Was 256 KB, which dropped every capture for exactly the users worth
# capturing. A real production payload for an active user measured 520 KB: a
# ~113k-token prompt plus 61 tool schemas (39 KB) and the system prompt
# (13 KB). Their newest stored capture was two days stale during an incident,
# because every agent-main call since had logged "dropped (oversize)".
#
# 2 MiB is sized off the trim ceiling rather than that one observation. OSS
# trims the prompt once it exceeds ``context_trim_trigger_tokens`` (default
# 150k) and drops it back to ``context_trim_target_tokens`` (default 120k), so
# at roughly 4 bytes per token a heavy user rests near 480 KB and peaks near
# 600 KB, plus tool schemas and system prompt. 2 MiB leaves ~3x headroom over
# that peak while still catching a genuine runaway.
#
# The trim thresholds bound the round-0 prompt, not every capture: the agent's
# tool-round loop appends tool results without re-trimming (up to
# ``max_tool_rounds``), and each round emits its own agent-main payload. The
# real backstop for those is the model's context window, so re-check this
# constant when switching to a much larger-context model.
#
# Storage: the cap applies independently to the request and response halves of
# each of the two eras, so a single user's row bounds at 4 x 2 MiB = 8 MiB
# worst case (responses are output-token-bounded in practice, so realistically
# far less). Postgres stores those JSONB values out-of-line via TOAST.
MAX_CAPTURE_BYTES = 2 * 1024 * 1024

# Bounded retry for pairing a response with its request capture. The
# request and response observers each spawn their own background task,
# so a fast LLM round can fire the response task before the request
# task has committed. Three attempts on exponential backoff (50ms,
# 100ms, 200ms) covers the realistic commit latency without delaying
# the captures' background-task work meaningfully.
_RESPONSE_PAIRING_ATTEMPTS = 3
_RESPONSE_PAIRING_BACKOFF_S = 0.05


# Strong references to in-flight background tasks. ``asyncio.create_task``
# only weakly references the task it spawns; without a strong reference,
# the GC can collect a still-pending task and silently drop the work.
# Tasks remove themselves from the set when they complete.
_pending_tasks: set[asyncio.Task[None]] = set()


def _payload_to_json_dict(payload: LLMRequestPayload) -> dict[str, Any]:
    """Serialize ``LLMRequestPayload`` into a JSON-safe dict for JSONB storage."""
    return {
        "schema_version": payload.schema_version,
        "user_id": payload.user_id,
        "session_id": payload.session_id,
        "request_id": payload.request_id,
        "model": payload.model,
        "provider": payload.provider,
        "max_tokens": payload.max_tokens,
        "thinking": payload.thinking,
        "system": payload.system,
        "messages": payload.messages,
        "tools": payload.tools,
        "min_message_seq_in_prompt": payload.min_message_seq_in_prompt,
        "started_at": payload.started_at.isoformat(),
    }


async def capture_llm_request(payload: LLMRequestPayload) -> None:
    """Persist ``payload`` if the user has opted in and the purpose is
    ``PURPOSE_AGENT_MAIN``.

    Used directly by tests (so they can ``await`` and observe the row).
    The OSS observer is a thin wrapper, registered by
    ``install_llm_payload_capture``, that fires this onto a background
    task to keep the agent loop off the round-trip critical path.

    Failure modes are all logged and swallowed; the worker task must
    not be allowed to propagate a CancelledError or DB error back to
    the caller.
    """
    if payload.purpose != PURPOSE_AGENT_MAIN:
        # Defense-in-depth: the observer entrypoint filters too, but
        # tests and any future direct caller see the same contract.
        return
    try:
        await _capture_llm_request_inner(payload)
    except Exception:
        logger.exception("llm_payload_capture failed", extra={"user_id": payload.user_id})


async def _capture_llm_request_inner(payload: LLMRequestPayload) -> None:
    serialized = _payload_to_json_dict(payload)
    encoded_bytes = len(json.dumps(serialized, separators=(",", ":")).encode("utf-8"))
    if encoded_bytes > MAX_CAPTURE_BYTES:
        # WARNING, not INFO: a drop means the payload-capture tool is silently
        # not working for this user, and the stored capture is now stale by
        # however long the drops have been running. That is the state you least
        # want to discover mid-incident from an INFO line.
        logger.warning(
            "llm_payload_capture dropped (oversize)",
            extra={
                "user_id": payload.user_id,
                "bytes": encoded_bytes,
                "limit": MAX_CAPTURE_BYTES,
            },
        )
        return

    async with db_session_async() as db:
        consent_row = (
            await db.execute(select(User.data_sharing_consent).where(User.id == payload.user_id))
        ).scalar_one_or_none()
        if not consent_row:
            # User does not exist OR has not opted in. If a row already
            # exists for them, lazily clean it up: this is the only
            # cleanup path for users who revoke consent and then make
            # another LLM call (we can't hook OSS's consent toggle
            # without expanding the OSS surface area further).
            await purge_user_captures(db, payload.user_id)
            await db.commit()
            logger.debug(
                "llm_payload_capture skipped (no consent)",
                extra={"user_id": payload.user_id},
            )
            return

        captured_at = datetime.now(UTC)
        new_min_seq = payload.min_message_seq_in_prompt

        stmt = pg_insert(LLMPayloadCapture).values(
            user_id=payload.user_id,
            current_era_payload=serialized,
            current_era_min_message_seq=new_min_seq,
            current_era_captured_at=captured_at,
            current_era_request_id=payload.request_id,
            current_era_payload_bytes=encoded_bytes,
            previous_era_payload=None,
            previous_era_min_message_seq=None,
            previous_era_captured_at=None,
            previous_era_request_id=None,
            previous_era_payload_bytes=None,
        )

        # Era rotation: when the existing row's era marker differs from
        # the incoming one, the existing current_* fields are rotated
        # into previous_*. ``IS DISTINCT FROM`` treats NULL and any
        # integer as different, so a fresh-session capture (NULL seq)
        # rotates correctly into a real-seq era when the next capture
        # arrives.
        c = LLMPayloadCapture.__table__.c
        ex = stmt.excluded
        era_changed = c.current_era_min_message_seq.is_distinct_from(ex.current_era_min_message_seq)

        set_map: dict[str, Any] = {
            "previous_era_payload": sa_case(
                (era_changed, c.current_era_payload),
                else_=c.previous_era_payload,
            ),
            "previous_era_min_message_seq": sa_case(
                (era_changed, c.current_era_min_message_seq),
                else_=c.previous_era_min_message_seq,
            ),
            "previous_era_captured_at": sa_case(
                (era_changed, c.current_era_captured_at),
                else_=c.previous_era_captured_at,
            ),
            "previous_era_request_id": sa_case(
                (era_changed, c.current_era_request_id),
                else_=c.previous_era_request_id,
            ),
            "previous_era_payload_bytes": sa_case(
                (era_changed, c.current_era_payload_bytes),
                else_=c.previous_era_payload_bytes,
            ),
            "current_era_payload": ex.current_era_payload,
            "current_era_min_message_seq": ex.current_era_min_message_seq,
            "current_era_captured_at": ex.current_era_captured_at,
            "current_era_request_id": ex.current_era_request_id,
            "current_era_payload_bytes": ex.current_era_payload_bytes,
        }
        upsert_stmt = stmt.on_conflict_do_update(index_elements=[c.user_id], set_=set_map)

        await db.execute(upsert_stmt)
        await db.commit()
        # Debug-level so the size distribution can be monitored without
        # spamming production logs. Useful for tuning ``MAX_CAPTURE_BYTES``
        # against real-world payloads.
        logger.debug(
            "llm_payload_capture written",
            extra={
                "user_id": payload.user_id,
                "bytes": encoded_bytes,
                "min_seq": new_min_seq,
                "purpose": payload.purpose,
            },
        )


async def purge_user_captures(db: AsyncSession, user_id: str) -> None:
    """Delete the capture row for ``user_id`` if one exists.

    Used both by the observer's lazy-cleanup path (when a user is
    captured-eligible by consent dropping to False) and by the admin
    export endpoint (defense-in-depth, in case consent dropped between
    last capture and current read). Caller is responsible for
    committing the surrounding transaction.
    """
    await db.execute(delete(LLMPayloadCapture).where(LLMPayloadCapture.user_id == user_id))


async def _observer_entrypoint(payload: LLMRequestPayload) -> None:
    """OSS-facing observer. Filters by purpose and fires-and-forgets.

    Returns immediately (a few attribute reads + one ``create_task``) so
    the agent loop is not on the critical path for the DB round-trip.
    The spawned task carries its own try/except via ``capture_llm_request``.
    """
    if payload.purpose != PURPOSE_AGENT_MAIN:
        # Compaction / heartbeat / agent-followup don't carry a meaningful
        # era marker; capturing them would corrupt the rotation. The capture
        # can grow per-purpose capture surfaces in a follow-up if there's
        # demand.
        return
    task = asyncio.create_task(capture_llm_request(payload))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


# ---------------------------------------------------------------------------
# Response side
# ---------------------------------------------------------------------------


def _response_to_json_dict(payload: LLMResponsePayload) -> dict[str, Any]:
    """Serialize ``LLMResponsePayload`` into a JSON-safe dict for JSONB storage."""
    return {
        "schema_version": payload.schema_version,
        "user_id": payload.user_id,
        "session_id": payload.session_id,
        "request_id": payload.request_id,
        "model": payload.model,
        "provider": payload.provider,
        "content_blocks": payload.content_blocks,
        "stop_reason": payload.stop_reason,
        "input_tokens": payload.input_tokens,
        "output_tokens": payload.output_tokens,
        "cache_creation_input_tokens": payload.cache_creation_input_tokens,
        "cache_read_input_tokens": payload.cache_read_input_tokens,
        "started_at": payload.started_at.isoformat(),
        "completed_at": payload.completed_at.isoformat(),
    }


async def capture_llm_response(payload: LLMResponsePayload) -> None:
    """Attach ``payload`` to the matching request capture, if one exists.

    Match strategy: ``(user_id, request_id)``. Looks at the current era
    first, then the previous era (for the rare case where a request just
    rotated between capture and response). Drops on no match -- the
    request was either filtered (purpose != AGENT_MAIN), missing consent,
    oversized, or rotated out before we could pair them. Dropping the
    orphan response is safer than inserting a row with NULL request
    columns: an admin reading the export would have no way to tell why
    only half the pair landed.

    Same error-swallowing contract as ``capture_llm_request``: this is
    invoked from a background task, must never raise.
    """
    if payload.purpose != PURPOSE_AGENT_MAIN:
        return
    if payload.request_id is None:
        # No way to pair: emit_llm_request also stores request_id, so
        # an absent value means the OSS layer didn't tag this round.
        # Logging at DEBUG keeps prod noise down while staying searchable.
        logger.debug(
            "llm_response_capture skipped (no request_id)",
            extra={"user_id": payload.user_id},
        )
        return
    try:
        await _capture_llm_response_inner(payload)
    except Exception:
        logger.exception("llm_response_capture failed", extra={"user_id": payload.user_id})


async def _capture_llm_response_inner(payload: LLMResponsePayload) -> None:
    serialized = _response_to_json_dict(payload)
    encoded_bytes = len(json.dumps(serialized, separators=(",", ":")).encode("utf-8"))
    if encoded_bytes > MAX_CAPTURE_BYTES:
        logger.info(
            "llm_response_capture dropped (oversize)",
            extra={
                "user_id": payload.user_id,
                "bytes": encoded_bytes,
                "limit": MAX_CAPTURE_BYTES,
            },
        )
        return

    captured_at = datetime.now(UTC)
    # We don't re-check ``data_sharing_consent`` here. The request half
    # of the pair already gated on consent, so an existing row implies
    # an opted-in user. If consent was revoked after the request landed,
    # the row is purged on the next request emit (lazy cleanup), and any
    # orphan response just misses its pair.
    #
    # Bounded retry: the request capture task may not have committed yet
    # (both observers fire-and-forget into separate tasks, so a fast LLM
    # round can fire the response before the request lands). Each attempt
    # is two atomic UPDATEs keyed on ``(user_id, request_id)``, so era
    # rotation between SELECT and UPDATE can no longer write to the wrong
    # era: a rotation simply makes the UPDATE match zero rows.
    assert payload.request_id is not None  # gated by ``capture_llm_response``
    for attempt in range(_RESPONSE_PAIRING_ATTEMPTS):
        if await _attempt_pair_response(
            payload.user_id,
            payload.request_id,
            serialized,
            captured_at,
            encoded_bytes,
        ):
            logger.debug(
                "llm_response_capture written",
                extra={
                    "user_id": payload.user_id,
                    "request_id": payload.request_id,
                    "bytes": encoded_bytes,
                    "attempt": attempt + 1,
                },
            )
            return
        if attempt < _RESPONSE_PAIRING_ATTEMPTS - 1:
            await asyncio.sleep(_RESPONSE_PAIRING_BACKOFF_S * (2**attempt))

    # All attempts missed: either the request was never captured
    # (purpose / consent / oversize filter), or it rotated off the row
    # while we were retrying, or the response observer arrived before
    # the request observer even though we waited.
    logger.debug(
        "llm_response_capture skipped (no matching request after %d attempts)",
        _RESPONSE_PAIRING_ATTEMPTS,
        extra={"user_id": payload.user_id, "request_id": payload.request_id},
    )


async def _attempt_pair_response(
    user_id: str,
    request_id: str,
    serialized: dict[str, Any],
    captured_at: datetime,
    encoded_bytes: int,
) -> bool:
    """Try to attach the response to the matching era in a single SQL round-trip.

    Two atomic UPDATEs keyed on ``(user_id, request_id)``: current era
    first, then previous era. A concurrent rotation between the two
    UPDATEs is harmless because the second UPDATE's WHERE clause still
    only matches the era whose request_id was equal at write time.
    Returns ``True`` when either UPDATE wrote a row.
    """
    async with db_session_async() as db:
        result = await db.execute(
            update(LLMPayloadCapture)
            .where(
                LLMPayloadCapture.user_id == user_id,
                LLMPayloadCapture.current_era_request_id == request_id,
            )
            .values(
                current_era_response=serialized,
                current_era_response_captured_at=captured_at,
                current_era_response_bytes=encoded_bytes,
            )
        )
        if cast("CursorResult[object]", result).rowcount > 0:
            await db.commit()
            return True
        result = await db.execute(
            update(LLMPayloadCapture)
            .where(
                LLMPayloadCapture.user_id == user_id,
                LLMPayloadCapture.previous_era_request_id == request_id,
            )
            .values(
                previous_era_response=serialized,
                previous_era_response_captured_at=captured_at,
                previous_era_response_bytes=encoded_bytes,
            )
        )
        if cast("CursorResult[object]", result).rowcount > 0:
            await db.commit()
            return True
    return False


async def _response_observer_entrypoint(payload: LLMResponsePayload) -> None:
    """OSS-facing response observer. Filters by purpose and fires-and-forgets.

    Mirrors ``_observer_entrypoint`` for the request side: cheap
    returns-immediately filter + ``create_task`` so the agent loop is
    never on the DB round-trip critical path.
    """
    if payload.purpose != PURPOSE_AGENT_MAIN:
        return
    task = asyncio.create_task(capture_llm_response(payload))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def install_llm_payload_capture() -> None:
    """Register the capture observers with the agent observer hooks."""
    set_llm_request_observer(_observer_entrypoint)
    set_llm_response_observer(_response_observer_entrypoint)
    logger.info("LLM payload capture observers installed (request + response)")
