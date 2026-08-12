"""Note tools for AppFolio work orders, including photo attachments.

Notes are the vendor's primary write surface inside a work order:
status updates, "arrived on site", "needs another visit", with photos
attached as base64 entries in the JSON body. Both add and update flow
through the shared :mod:`media_resolver` so the agent can reference
photos by ``original_url`` or by handle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.concurrency import work_order_concurrency_key
from backend.app.integrations.appfolio_vendor.errors import (
    log_unexpected_response_shape,
    service_error_to_tool_result,
)
from backend.app.integrations.appfolio_vendor.media_resolver import resolve_staged_files
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioAddNoteParams,
    AppFolioListNotesParams,
    AppFolioUpdateNoteParams,
)
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


_KNOWN_NOTE_LIST_ENVELOPES = ("notes", "data", "results")


def _normalize_notes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [n for n in payload if isinstance(n, dict)]
    if isinstance(payload, dict):
        for key in _KNOWN_NOTE_LIST_ENVELOPES:
            value = payload.get(key)
            if isinstance(value, list):
                return [n for n in value if isinstance(n, dict)]
    return []


def build_note_tools(service: AppFolioVendorService, ctx: ToolContext) -> list[Tool]:
    """Return the AppFolio work-order note tools."""

    # Note ids created during this agent run, keyed by work order. The tool
    # list is rebuilt per inbound message, so this is scoped to one user
    # turn (spanning every LLM round of that turn) and never leaks between
    # messages or users.
    #
    # appfolio_update_note consults it to refuse a note_id the model
    # predicted rather than read back from an appfolio_add_note result.
    # Serializing the two writes fixes the ordering, but a predicted id
    # that lands on a different real note overwrites that note's body,
    # which the PM sees and the vendor does not.
    added_note_ids: dict[str, set[str]] = {}

    async def appfolio_list_notes(work_order_id: str) -> ToolResult:
        try:
            payload = await service.list_work_order_notes(work_order_id)
        except Exception as exc:
            return service_error_to_tool_result("listing notes", exc)
        notes = _normalize_notes(payload)
        if not notes:
            if isinstance(payload, dict) and payload:
                log_unexpected_response_shape(
                    f"appfolio_list_notes(work_order_id={work_order_id})",
                    payload,
                    expected=(
                        "list of note dicts, or a dict with one of "
                        f"{list(_KNOWN_NOTE_LIST_ENVELOPES)} containing the list"
                    ),
                )
            return ToolResult(content=f"No notes on work order {work_order_id}.")
        lines = [f"{len(notes)} note(s) on work order {work_order_id}:"]
        for n in notes[:30]:
            note_id = n.get("id") or "?"
            created = n.get("created_at") or n.get("createdAt") or ""
            text = (n.get("body") or "").strip().replace("\n", " ")[:160]
            lines.append(f"- ID: {note_id} | {created} | {text}")
        return ToolResult(content="\n".join(lines))

    async def appfolio_add_note(
        work_order_id: str,
        body: str,
        media_refs: list[str],
    ) -> ToolResult:
        text = body.strip()
        if not text:
            return ToolResult(
                content="Note body cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        files_or_err = await resolve_staged_files(ctx, media_refs)
        if isinstance(files_or_err, ToolResult):
            return files_or_err
        files = files_or_err
        try:
            result = await service.add_work_order_note(
                work_order_id, body_text=text, files=files or None
            )
        except Exception as exc:
            return service_error_to_tool_result("adding note", exc)
        note_id = ""
        if isinstance(result, dict):
            note_id = str(result.get("id") or result.get("note", {}).get("id") or "")
        if note_id:
            added_note_ids.setdefault(work_order_id, set()).add(note_id)
        photo_count = len(files)
        photo_phrase = f" with {photo_count} photo(s)" if photo_count else ""
        return ToolResult(
            content=(
                f"Added note{photo_phrase} to work order {work_order_id}"
                + (f" (note id {note_id})." if note_id else ".")
            ),
            receipt=ToolReceipt(
                action="Added AppFolio work order note",
                target=f"#{work_order_id}{photo_phrase}",
            ),
        )

    async def appfolio_update_note(
        work_order_id: str,
        note_id: str,
        body: str,
        media_refs: list[str],
    ) -> ToolResult:
        text = body.strip()
        if not text:
            return ToolResult(
                content="Note body cannot be empty.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        # When this turn added notes to this work order, the only ids the
        # model can legitimately edit are the ones add_note handed back.
        # Anything else is a prediction. Only enforced when we actually
        # captured an id: AppFolio does not always return one, and an
        # empty set must not block a legitimate edit.
        turn_added = added_note_ids.get(work_order_id)
        if turn_added and note_id.strip() not in turn_added:
            known = ", ".join(sorted(turn_added))
            logger.warning(
                "appfolio_update_note_unverified_id work_order=%s requested=%s added=%s",
                work_order_id,
                note_id,
                known,
            )
            return ToolResult(
                content=(
                    f"Refusing to edit note {note_id} on work order {work_order_id}: "
                    f"this turn added note {known}, and {note_id} is not among them. "
                    "Edit one of the ids returned by appfolio_add_note, or list the "
                    "notes with appfolio_list_notes to find the right one."
                ),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        files_or_err = await resolve_staged_files(ctx, media_refs)
        if isinstance(files_or_err, ToolResult):
            return files_or_err
        files = files_or_err
        try:
            await service.update_work_order_note(
                work_order_id, note_id, body_text=text, files=files or None
            )
        except Exception as exc:
            return service_error_to_tool_result("updating note", exc)
        photo_count = len(files)
        photo_phrase = f", added {photo_count} photo(s)" if photo_count else ""
        return ToolResult(
            content=f"ok | work order: #{work_order_id} | note: {note_id}{photo_phrase}",
            receipt=ToolReceipt(
                action="Updated AppFolio work order note",
                target=f"#{work_order_id} note {note_id}{photo_phrase}",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_LIST_NOTES,
            description="List notes on an AppFolio work order.",
            function=appfolio_list_notes,
            params_model=AppFolioListNotesParams,
            usage_hint="Use to see prior status updates and photos before adding a new one.",
        ),
        Tool(
            name=ToolName.APPFOLIO_ADD_NOTE,
            description=("Add a note (text + optional photos) to an AppFolio work order."),
            function=appfolio_add_note,
            params_model=AppFolioAddNoteParams,
            concurrency_group=work_order_concurrency_key,
            usage_hint=(
                "Pass photos by their original_url from the conversation or by"
                " media handle from analyze_photo. Notes are visible to the PM."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    "Add a note to AppFolio work order"
                    f" #{args.get('work_order_id', '?')}"
                    + (
                        f" with {len(args.get('media_refs') or [])} photo(s)"
                        if args.get("media_refs")
                        else ""
                    )
                ),
            ),
        ),
        Tool(
            name=ToolName.APPFOLIO_UPDATE_NOTE,
            description=(
                "Edit an existing AppFolio work-order note. Pass the note_id returned"
                " by appfolio_add_note or appfolio_list_notes; never predict one."
            ),
            function=appfolio_update_note,
            params_model=AppFolioUpdateNoteParams,
            concurrency_group=work_order_concurrency_key,
            usage_hint="Only use when the user wants to fix the text or attach more photos.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Update AppFolio note {args.get('note_id', '?')}"
                    f" on work order #{args.get('work_order_id', '?')}"
                ),
            ),
        ),
    ]
