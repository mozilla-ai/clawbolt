"""Gmail tools for the agent.

Registers four tools (``gmail_search``, ``gmail_get_message``,
``gmail_list_recent``, ``gmail_send``). All four default to ``ask`` permission
because reading mail and sending mail are both privacy-sensitive: the user
should explicitly allow each operation rather than letting the LLM act
autonomously on their inbox.

The factory mirrors ``calendar/factory.py`` and lives off the same shared
``oauth_service`` token store. The ``auth_check`` returns ``None`` (i.e. the
integration is hidden entirely) when the deployment has not configured the
Gmail OAuth client, matching the Calendar pattern.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.integrations.gmail.service import (
    GmailMessage,
    GmailMessageSummary,
    GmailService,
)
from backend.app.services.oauth import oauth_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class GmailSearchParams(BaseModel):
    """Parameters for the gmail_search tool."""

    query: str = Field(
        description=(
            "Gmail search query, using Gmail's native syntax. Examples: "
            "'from:noreply@appfolio.com newer_than:1d', 'subject:invoice', "
            "'is:unread', 'has:attachment'."
        ),
    )
    max_results: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of messages to return (1-50). Default 10.",
    )


class GmailGetMessageParams(BaseModel):
    """Parameters for the gmail_get_message tool."""

    message_id: str = Field(
        description="The Gmail message ID returned by gmail_search or gmail_list_recent.",
    )


class GmailListRecentParams(BaseModel):
    """Parameters for the gmail_list_recent tool."""

    max_results: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of recent messages to return (1-50). Default 10.",
    )


class GmailSendParams(BaseModel):
    """Parameters for the gmail_send tool."""

    to: list[str] = Field(
        description=(
            "Recipient email addresses (one or more). Each entry may be a "
            "bare address ('jane@example.com') or a name+address pair "
            "('Jane Doe <jane@example.com>')."
        ),
    )
    subject: str = Field(description="Subject line of the email.")
    body: str = Field(description="Plain-text body of the email.")
    reply_to_message_id: str = Field(
        default="",
        description=(
            "Optional Gmail message ID to reply to. When set, the new message "
            "is threaded onto the original conversation and the In-Reply-To / "
            "References headers are populated automatically. Leave empty to "
            "send a brand-new message."
        ),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_summary(s: GmailMessageSummary) -> str:
    parts = [s.sender or "(unknown sender)"]
    if s.subject:
        parts.append(s.subject)
    else:
        parts.append("(no subject)")
    if s.date:
        parts.append(s.date)
    if s.snippet:
        snippet = s.snippet[:140]
        if len(s.snippet) > 140:
            snippet += "..."
        parts.append(snippet)
    parts.append(f"[id: {s.id}]")
    return " | ".join(parts)


def _format_message(m: GmailMessage) -> str:
    lines = [
        f"From: {m.sender or '(unknown)'}",
        f"To: {', '.join(m.recipients) or '(none)'}",
    ]
    if m.cc:
        lines.append(f"Cc: {', '.join(m.cc)}")
    lines.append(f"Subject: {m.subject or '(no subject)'}")
    if m.date:
        lines.append(f"Date: {m.date}")
    lines.append(f"Message ID: {m.id}")
    if m.thread_id and m.thread_id != m.id:
        lines.append(f"Thread ID: {m.thread_id}")
    if m.links:
        lines.append("Links found in body:")
        for url in m.links:
            lines.append(f"  - {url}")
    lines.append("")
    lines.append("Body:")
    lines.append(m.body or "(empty body)")
    return "\n".join(lines)


def _parse_gmail_error(body: str) -> tuple[str, str]:
    """Return ``(message, reason)`` from a Gmail JSON error body.

    Gmail's REST error shape is
    ``{"error": {"code": ..., "message": "...", "errors": [{"reason": "..."}]}}``.
    Returns empty strings when the body is missing, not JSON, or lacks the
    expected shape so callers can fall through to a generic message.
    """
    if not body:
        return "", ""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "", ""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return "", ""
    message = err.get("message") or ""
    reason = ""
    errors = err.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason") or ""
    return message, reason


# Cap Gmail's message text so a verbose 403 (which can include a console URL
# and several sentences) doesn't blow past a reasonable line length when the
# LLM renders it back to the user.
_MAX_GMAIL_MESSAGE_CHARS = 300


def _format_gmail_message(message: str) -> str:
    if len(message) <= _MAX_GMAIL_MESSAGE_CHARS:
        return message
    return message[:_MAX_GMAIL_MESSAGE_CHARS].rstrip() + "..."


def _handle_http_error(exc: httpx.HTTPStatusError, action: str) -> ToolResult:
    status = exc.response.status_code
    body = ""
    with contextlib.suppress(Exception):
        body = exc.response.text[:1000]
    logger.warning(
        "Gmail HTTP %d during %s: url=%s body=%s",
        status,
        action,
        str(exc.request.url) if exc.request else "unknown",
        body,
    )
    message, reason = _parse_gmail_error(body)
    if status == 401:
        return ToolResult(
            content="Gmail disconnected. Please reconnect Gmail in Settings.",
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
        )
    if status == 403:
        # 403 has many causes: insufficientPermissions (real scope problem,
        # reconnect helps), accessNotConfigured (Gmail API disabled in the
        # GCP project, operator must enable it), domainPolicy (Workspace
        # admin block), failedPrecondition, etc. We surface Gmail's own
        # message verbatim so the user sees the actual cause instead of a
        # canned "reconnect" guess that's wrong most of the time.
        if reason == "insufficientPermissions":
            content = (
                f"Permission denied while trying to {action}. "
                "The Gmail integration is missing a required scope; "
                "disconnect and reconnect Gmail to grant the missing permissions."
            )
        elif message:
            content = (
                f"Gmail denied the request while trying to {action}: "
                f"{_format_gmail_message(message)}"
            )
        else:
            content = f"Gmail denied the request while trying to {action} (HTTP 403)."
        return ToolResult(
            content=content,
            is_error=True,
            error_kind=ToolErrorKind.PERMISSION,
        )
    if status == 404:
        return ToolResult(
            content=(
                f"Not found while trying to {action}. The message may have been "
                "deleted or the ID is wrong."
            ),
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )
    if status == 429:
        return ToolResult(
            content="Gmail rate limited. Try again shortly.",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
            hint="Wait a moment before retrying Gmail operations.",
        )
    return ToolResult(
        content=f"Gmail service error ({status}) while trying to {action}.",
        is_error=True,
        error_kind=ToolErrorKind.SERVICE,
    )


# ---------------------------------------------------------------------------
# Tool creation
# ---------------------------------------------------------------------------


def create_gmail_tools(service: GmailService) -> list[Tool]:
    """Create Gmail tools bound to a service instance."""

    async def _run_search(query: str, max_results: int, empty_msg: str) -> ToolResult:
        try:
            results = await service.search_messages(query, max_results)
        except httpx.TimeoutException:
            return ToolResult(
                content="Gmail unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, "search messages")
        except Exception as exc:
            logger.exception("Gmail search failed")
            return ToolResult(
                content=f"Gmail error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not results:
            return ToolResult(content=empty_msg)
        lines = [f"Found {len(results)} message(s):"]
        for s in results:
            lines.append(f"- {_format_summary(s)}")
        return ToolResult(content="\n".join(lines))

    async def gmail_search(query: str, max_results: int = 10) -> ToolResult:
        return await _run_search(query, max_results, f"No messages match '{query}'.")

    async def gmail_get_message(message_id: str) -> ToolResult:
        try:
            msg = await service.get_message(message_id)
        except httpx.TimeoutException:
            return ToolResult(
                content="Gmail unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, f"get message {message_id}")
        except Exception as exc:
            logger.exception("Gmail get_message failed")
            return ToolResult(
                content=f"Gmail error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        return ToolResult(content=_format_message(msg))

    async def gmail_list_recent(max_results: int = 10) -> ToolResult:
        # Empty query lists in reverse-chronological order, matching the
        # Gmail web UI's default inbox view.
        return await _run_search("", max_results, "Inbox is empty.")

    async def gmail_send(
        to: list[str],
        subject: str,
        body: str,
        reply_to_message_id: str = "",
    ) -> ToolResult:
        if not to:
            return ToolResult(
                content="At least one recipient address is required.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        try:
            result = await service.send_message(
                to=to,
                subject=subject,
                body=body,
                reply_to_message_id=reply_to_message_id,
            )
        except httpx.TimeoutException:
            return ToolResult(
                content="Gmail unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, "send message")
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_kind=ToolErrorKind.VALIDATION)
        except Exception as exc:
            logger.exception("Gmail send failed")
            return ToolResult(
                content=f"Gmail error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        # Receipt strings get rendered to the user, so keep them short.
        receipt_target = f"reply to {reply_to_message_id}" if reply_to_message_id else ", ".join(to)
        # Content stays terse and data-only so the model has no
        # receipt-shaped phrasing to bullet-point in its reply.
        return ToolResult(
            content=f"ok | id: {result.id} | thread: {result.thread_id}",
            receipt=ToolReceipt(
                action="Sent email via Gmail",
                target=receipt_target,
            ),
        )

    return [
        Tool(
            name=ToolName.GMAIL_SEARCH,
            description=(
                "Search the user's Gmail inbox using Gmail's native query "
                "syntax (e.g. 'from:noreply@appfolio.com', 'subject:invoice', "
                "'is:unread', 'newer_than:7d'). Returns a list of message "
                "summaries (sender, subject, date, snippet, id)."
            ),
            function=gmail_search,
            params_model=GmailSearchParams,
            usage_hint=(
                "Use this when the user wants to find a specific email or "
                "set of emails. Combine multiple Gmail operators in a single "
                "query for precise results. Always confirm before opening "
                "anything sensitive."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: f"Search Gmail for: {args.get('query', '')}",
            ),
        ),
        Tool(
            name=ToolName.GMAIL_GET_MESSAGE,
            description=(
                "Fetch the full body of a single Gmail message by its ID. "
                "Returns headers, the plain-text body, and a deduplicated "
                "list of URLs found in the body."
            ),
            function=gmail_get_message,
            params_model=GmailGetMessageParams,
            usage_hint=(
                "Use after gmail_search or gmail_list_recent to read the "
                "contents of a specific message. The 'links' list is the "
                "fastest way to extract a magic link or unsubscribe URL."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: f"Read Gmail message {args.get('message_id', '')}",
            ),
        ),
        Tool(
            name=ToolName.GMAIL_LIST_RECENT,
            description=(
                "List the most recent messages in the user's Gmail inbox. "
                "Returns the same summary shape as gmail_search."
            ),
            function=gmail_list_recent,
            params_model=GmailListRecentParams,
            usage_hint=(
                "Use when the user asks 'what's in my inbox' or wants a "
                "general overview without a specific search query."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"List the {args.get('max_results', 10)} most recent Gmail messages"
                ),
            ),
        ),
        Tool(
            name=ToolName.GMAIL_SEND,
            description=(
                "Send an email from the user's Gmail account. Pass "
                "reply_to_message_id to thread the new message onto an "
                "existing conversation (the original headers and threadId "
                "are wired up for you)."
            ),
            function=gmail_send,
            params_model=GmailSendParams,
            usage_hint=(
                "Use when the user explicitly asks you to send or reply to "
                "an email. Confirm recipients, subject, and body in chat "
                "before calling this tool. Always default to plain text."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    "Reply via Gmail"
                    if args.get("reply_to_message_id")
                    else f"Send Gmail to {', '.join(args.get('to', []) or [])}"
                ),
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Factory and registration
# ---------------------------------------------------------------------------


async def _gmail_auth_check(ctx: ToolContext) -> str | None:
    """Return ``None`` when Gmail is ready or not configured at all.

    Matches the calendar pattern: if the operator has not provided
    ``GMAIL_CLIENT_ID`` / ``GMAIL_CLIENT_SECRET`` this returns ``None`` so
    the integration stays completely hidden from the agent. When the
    operator HAS configured Gmail but the user has not connected, we return
    a reason string so the registry surfaces "not connected" cleanly.
    """
    if not settings.gmail_client_id or not settings.gmail_client_secret:
        return None
    token = await oauth_service.load_token(ctx.user.id, "gmail")
    if token is not None and token.access_token:
        return None
    return (
        "Gmail is not connected. "
        "Use manage_integration(action='connect', target='gmail') "
        "to generate a connection link for the user."
    )


async def _gmail_factory(ctx: ToolContext) -> list[Tool]:
    if not settings.gmail_client_id or not settings.gmail_client_secret:
        return []
    token = await oauth_service.get_valid_token(ctx.user.id, "gmail")
    if token is None or not token.access_token:
        return []
    service = GmailService(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        token_expires_at=token.expires_at or 0.0,
        on_token_refresh=oauth_service.build_on_refresh_callback(ctx.user.id, "gmail"),
    )
    return create_gmail_tools(service)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "gmail",
        _gmail_factory,
        core=False,
        summary=("Search, read, and send Gmail messages on the user's behalf"),
        display_name="Gmail",
        dashboard_description="Search, read, and send Gmail messages on the user's behalf",
        dashboard_group="Integrations",
        dashboard_group_order=2,
        sub_tools=[
            SubToolInfo(
                ToolName.GMAIL_SEARCH,
                "Search the inbox using Gmail's native query syntax",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.GMAIL_GET_MESSAGE,
                "Read the full body of a single Gmail message",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.GMAIL_LIST_RECENT,
                "List the most recent messages in the inbox",
                default_permission="ask",
            ),
            SubToolInfo(
                ToolName.GMAIL_SEND,
                "Send an email or threaded reply from the user's Gmail",
                default_permission="ask",
            ),
        ],
        auth_check=_gmail_auth_check,
    )


_register()
