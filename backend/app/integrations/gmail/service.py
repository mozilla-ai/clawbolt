"""Gmail REST API client using httpx.

Mirrors the shape of ``calendar/service.py``: an httpx-based client with a
proactive token refresh, reactive 401 retry, and a refresh callback so the
``oauth_service`` can persist rotated tokens.

Why a hand-rolled client and not ``google-api-python-client``: the rest of
this codebase is httpx-based, the surface area we need from Gmail is tiny
(three reads + one write), and the official client drags in synchronous
discovery-document fetches we'd have to wrap to keep ``async``.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh 5 minutes before expiry. Matches the calendar service so the two
# integrations behave identically when both are in use.
_REFRESH_BUFFER_SECONDS = 300

# Cap the body slice we surface to the LLM so a marketing newsletter doesn't
# eat the context window. Callers asking for "the magic link" only need the
# first chunk; full retrieval of long bodies is intentionally out of scope.
_MAX_BODY_CHARS = 16_000

# A Gmail search returning thousands of message IDs would be useless to the
# LLM and expensive to fetch. Hard ceiling at the API's per-page max of 500.
_MAX_RESULTS_CEILING = 500

_URL_RE = re.compile(r"https?://[^\s<>\"')]+")


@dataclass
class GmailMessageSummary:
    """Lightweight summary returned by search / list-recent."""

    id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    snippet: str


@dataclass
class GmailMessage:
    """Full message returned by ``get_message``.

    ``links`` is a deduplicated list of every URL the body contains, in
    first-seen order. The Gmail API returns the body either as plain text
    or as HTML depending on the part; we prefer ``text/plain`` and fall
    back to a stripped-tags rendering of ``text/html`` so the agent always
    has something readable to work with.
    """

    id: str
    thread_id: str
    sender: str
    recipients: list[str]
    cc: list[str]
    subject: str
    date: str
    body: str
    links: list[str] = field(default_factory=list)
    rfc822_message_id: str = ""


@dataclass
class GmailSendResult:
    """Outcome of a successful send."""

    id: str
    thread_id: str


class GmailService:
    """Gmail API client bound to one user's tokens."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        on_token_refresh: Callable[[str, str, float], Awaitable[None]] | None = None,
        token_expires_at: float = 0.0,
        sender_email: str = "",
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_token_refresh = on_token_refresh
        self._token_expires_at = token_expires_at
        # Resolved lazily on first ``send_message`` call so we don't pay
        # the round-trip on read-only flows. ``getProfile`` is the only
        # Gmail endpoint that returns the authenticated user's address;
        # the OAuth ``id_token`` is not in scope here.
        self._sender_email = sender_email

    @property
    def provider_name(self) -> str:
        return "gmail"

    # -- Token refresh --------------------------------------------------------

    async def _refresh_access_token(self, client: httpx.AsyncClient) -> None:
        logger.info("Refreshing Gmail access token")
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        if "expires_in" in data:
            self._token_expires_at = time.time() + data["expires_in"]
        if self._on_token_refresh:
            await self._on_token_refresh(
                self._access_token, self._refresh_token, self._token_expires_at
            )

    async def _ensure_valid_token(self, client: httpx.AsyncClient) -> None:
        if self._token_expires_at <= 0:
            return
        if time.time() >= (self._token_expires_at - _REFRESH_BUFFER_SECONDS):
            await self._refresh_access_token(client)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{GMAIL_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._ensure_valid_token(client)
            headers["Authorization"] = f"Bearer {self._access_token}"
            resp = await client.request(method, url, headers=headers, json=json, params=params)
            if resp.status_code == 401:
                await self._refresh_access_token(client)
                headers["Authorization"] = f"Bearer {self._access_token}"
                resp = await client.request(method, url, headers=headers, json=json, params=params)
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    # -- Public API -----------------------------------------------------------

    async def get_profile(self) -> dict[str, Any]:
        """Return ``users.me.getProfile`` (caches ``emailAddress`` for sends)."""
        data = await self._request("GET", "/users/me/profile") or {}
        if not self._sender_email:
            self._sender_email = data.get("emailAddress", "")
        return data

    async def search_messages(
        self,
        query: str,
        max_results: int,
    ) -> list[GmailMessageSummary]:
        """Run a Gmail search; returns light summaries for each hit.

        Gmail's ``messages.list`` only returns IDs, so we follow up with a
        ``metadata`` fetch per ID. We cap *max_results* at
        ``_MAX_RESULTS_CEILING`` so the agent can't accidentally hammer the
        API with a 10000-row query.
        """
        capped = max(1, min(int(max_results), _MAX_RESULTS_CEILING))
        params: dict[str, str] = {"maxResults": str(capped)}
        if query:
            params["q"] = query
        listing = await self._request("GET", "/users/me/messages", params=params) or {}
        ids = [item.get("id", "") for item in listing.get("messages", []) if item.get("id")]

        summaries: list[GmailMessageSummary] = []
        for msg_id in ids:
            try:
                summaries.append(await self._get_message_summary(msg_id))
            except httpx.HTTPStatusError as exc:
                # A single 404 (message deleted between list and get) shouldn't
                # nuke the whole response. Log + skip; the LLM still sees the rest.
                logger.warning(
                    "Skipping Gmail message %s during search: %s",
                    msg_id,
                    exc.response.status_code,
                )
                continue
        return summaries

    async def _get_message_summary(self, message_id: str) -> GmailMessageSummary:
        params = {
            "format": "metadata",
            "metadataHeaders": "From,Subject,Date",
        }
        data = await self._request("GET", f"/users/me/messages/{message_id}", params=params) or {}
        headers = _index_headers(data.get("payload", {}).get("headers", []))
        return GmailMessageSummary(
            id=data.get("id", ""),
            thread_id=data.get("threadId", ""),
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            snippet=data.get("snippet", ""),
        )

    async def get_message(self, message_id: str) -> GmailMessage:
        """Return the full message including a readable body and link list."""
        data = (
            await self._request(
                "GET", f"/users/me/messages/{message_id}", params={"format": "full"}
            )
            or {}
        )
        payload = data.get("payload", {})
        headers = _index_headers(payload.get("headers", []))
        body = _extract_body(payload)
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + "\n[...truncated]"
        links = _extract_links(body)
        return GmailMessage(
            id=data.get("id", ""),
            thread_id=data.get("threadId", ""),
            sender=headers.get("from", ""),
            recipients=_split_addresses(headers.get("to", "")),
            cc=_split_addresses(headers.get("cc", "")),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            body=body,
            links=links,
            rfc822_message_id=headers.get("message-id", ""),
        )

    async def send_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        reply_to_message_id: str = "",
    ) -> GmailSendResult:
        """Send a new message, optionally threading onto an existing message.

        When *reply_to_message_id* is non-empty we fetch the original message
        to pull its ``Message-ID`` and ``References`` headers so the reply
        threads correctly in Gmail (and any other RFC 5322 client). The
        ``threadId`` field on the send body is what makes Gmail's UI bundle
        the messages; the headers cover everyone else.
        """
        if not self._sender_email:
            await self.get_profile()
        if not to:
            raise ValueError("send_message requires at least one recipient")

        thread_id = ""
        in_reply_to = ""
        references = ""
        if reply_to_message_id:
            original = await self.get_message(reply_to_message_id)
            thread_id = original.thread_id
            in_reply_to = original.rfc822_message_id
            # Per RFC 5322 section 3.6.4, References is built by appending
            # the parent's Message-ID to the parent's References (or
            # In-Reply-To if References is absent). We don't have the
            # parent's References parsed out, so the simpler chain of
            # just the parent's ID is acceptable for a one-deep reply.
            references = in_reply_to

        rfc822 = _build_rfc822(
            sender=self._sender_email,
            to=to,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )
        raw_b64 = base64.urlsafe_b64encode(rfc822).decode("ascii")
        send_body: dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            send_body["threadId"] = thread_id

        data = await self._request("POST", "/users/me/messages/send", json=send_body) or {}
        return GmailSendResult(
            id=data.get("id", ""),
            thread_id=data.get("threadId", ""),
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _index_headers(headers: list[dict[str, Any]]) -> dict[str, str]:
    """Return a case-insensitive header lookup keyed by lowercase name."""
    out: dict[str, str] = {}
    for h in headers:
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = h.get("value", "")
    return out


def _extract_body(payload: dict[str, Any]) -> str:
    """Walk a Gmail ``payload`` tree and return the best readable body.

    Preference: ``text/plain`` over ``text/html``. When only ``text/html``
    is present we strip tags with a tiny regex so the agent gets something
    legible without dragging in BeautifulSoup. Multipart bodies are walked
    depth-first.
    """
    text_part = _find_part(payload, "text/plain")
    if text_part is not None:
        decoded = _decode_part_data(text_part)
        if decoded:
            return decoded
    html_part = _find_part(payload, "text/html")
    if html_part is not None:
        decoded = _decode_part_data(html_part)
        if decoded:
            return _strip_tags(decoded)
    # Fall back to the top-level body (common for short single-part messages).
    decoded = _decode_part_data(payload)
    return decoded or ""


def _find_part(payload: dict[str, Any], mime_type: str) -> dict[str, Any] | None:
    if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
        return payload
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def _decode_part_data(part: dict[str, Any]) -> str:
    body = part.get("body", {}) or {}
    data = body.get("data") or ""
    if not data:
        return ""
    try:
        # Gmail uses URL-safe base64 with no padding guarantees.
        padded = data + "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        logger.warning("Could not decode Gmail body part: %s", exc)
        return ""
    return raw.decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")
# Match the whole opening anchor tag (not just the href attribute) so the URL
# can be placed OUTSIDE the tag boundaries before the generic tag stripper
# runs; otherwise the hoisted URL would land inside ``<a ... URL >`` and be
# eaten by ``_TAG_RE``.
_ANCHOR_OPEN_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*['"]([^'"\s>]+)['"][^>]*>""", re.IGNORECASE)


def _strip_tags(html: str) -> str:
    """Best-effort HTML to text conversion.

    Anchor ``href`` URLs are surfaced inline before the tags get stripped, so
    a magic-link email rendered as ``<a href="https://x">click</a>`` still
    leaves the URL in the body for ``_extract_links`` to pick up.
    """
    hoisted = _ANCHOR_OPEN_RE.sub(r" \1 ", html)
    text = _TAG_RE.sub(" ", hoisted)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def _extract_links(body: str) -> list[str]:
    """Pull URLs out of a body in first-seen order, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.finditer(body):
        url = match.group(0).rstrip(".,);]>")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _split_addresses(value: str) -> list[str]:
    """Split a comma-separated address header into trimmed entries."""
    if not value:
        return []
    return [addr.strip() for addr in value.split(",") if addr.strip()]


def _build_rfc822(
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> bytes:
    """Build an RFC 5322 message ready for base64url Gmail send."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return bytes(msg)


__all__ = [
    "GmailMessage",
    "GmailMessageSummary",
    "GmailSendResult",
    "GmailService",
]
