"""Tests for the Gmail integration: service, tools, factory, and OAuth wiring."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.agent.approval import PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext
from backend.app.config import settings
from backend.app.integrations.gmail.factory import (
    _gmail_auth_check,
    _gmail_factory,
    create_gmail_tools,
)
from backend.app.integrations.gmail.service import (
    GmailMessage,
    GmailMessageSummary,
    GmailSendResult,
    GmailService,
    _build_rfc822,
    _extract_body,
    _extract_links,
    _strip_tags,
)
from backend.app.models import User
from backend.app.services.oauth import (
    GMAIL_SCOPES,
    get_gmail_oauth_config,
    get_oauth_config,
    list_oauth_integrations,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")


def _make_service(sender_email: str = "me@example.com") -> GmailService:
    return GmailService(
        access_token="test-access",
        refresh_token="test-refresh",
        client_id="cid",
        client_secret="csec",
        token_expires_at=time.time() + 3600,
        sender_email=sender_email,
    )


def _get_tool(tools: list[Tool], name: str) -> Tool:
    for t in tools:
        if t.name == name:
            return t
    msg = f"Tool {name} not found"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# OAuth config
# ---------------------------------------------------------------------------


def test_gmail_scopes_are_readonly_and_send() -> None:
    """v1 ships with exactly two Gmail scopes, no broader modify."""
    assert GMAIL_SCOPES == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]


def test_get_gmail_oauth_config_returns_none_when_unconfigured() -> None:
    with (
        patch.object(settings, "gmail_client_id", ""),
        patch.object(settings, "gmail_client_secret", ""),
    ):
        assert get_gmail_oauth_config() is None


def test_get_gmail_oauth_config_returns_config_when_set() -> None:
    with (
        patch.object(settings, "gmail_client_id", "gmail-cid"),
        patch.object(settings, "gmail_client_secret", "gmail-csec"),
    ):
        config = get_gmail_oauth_config()
    assert config is not None
    assert config.integration == "gmail"
    assert config.client_id == "gmail-cid"
    assert config.scopes == GMAIL_SCOPES
    assert config.use_pkce is False
    # access_type=offline is required to receive a refresh_token from Google.
    assert config.extra_auth_params == {"access_type": "offline", "prompt": "consent"}


def test_get_oauth_config_dispatches_gmail() -> None:
    with (
        patch.object(settings, "gmail_client_id", "gmail-cid"),
        patch.object(settings, "gmail_client_secret", "gmail-csec"),
    ):
        config = get_oauth_config("gmail")
    assert config is not None
    assert config.integration == "gmail"


def test_gmail_in_oauth_integrations_registry() -> None:
    assert "gmail" in list_oauth_integrations()


# ---------------------------------------------------------------------------
# Service: parsing helpers
# ---------------------------------------------------------------------------


def test_extract_body_prefers_text_plain() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain version")}},
            {"mimeType": "text/html", "body": {"data": _b64("<b>html version</b>")}},
        ],
    }
    assert _extract_body(payload) == "plain version"


def test_extract_body_falls_back_to_html_stripped() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<p>Hello <a href='https://x.com'>link</a></p>")},
    }
    body = _extract_body(payload)
    assert "Hello" in body
    assert "<p>" not in body


def test_extract_body_walks_nested_multipart() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("nested plain")}},
                ],
            }
        ],
    }
    assert _extract_body(payload) == "nested plain"


def test_extract_links_dedupes_in_order() -> None:
    body = "see https://a.com and https://b.com and https://a.com again"
    assert _extract_links(body) == ["https://a.com", "https://b.com"]


def test_extract_links_strips_trailing_punctuation() -> None:
    body = "click https://example.com/path."
    assert _extract_links(body) == ["https://example.com/path"]


def test_strip_tags_squashes_whitespace() -> None:
    out = _strip_tags("<div>  hello   <b>world</b>  </div>")
    assert "hello" in out
    assert "world" in out
    assert "<" not in out


def test_strip_tags_hoists_anchor_href_urls() -> None:
    """An HTML-only email with the magic link inside an <a href=...> must
    still surface the URL so _extract_links can find it."""
    html = '<p>Click <a href="https://magic.example/?token=abc">here</a> to log in</p>'
    out = _strip_tags(html)
    assert "https://magic.example/?token=abc" in out
    assert "<" not in out
    assert _extract_links(out) == ["https://magic.example/?token=abc"]


def test_build_rfc822_includes_threading_headers_when_set() -> None:
    raw = _build_rfc822(
        sender="me@example.com",
        to=["jane@example.com"],
        subject="Re: invoice",
        body="thanks",
        in_reply_to="<orig@mail.example.com>",
        references="<orig@mail.example.com>",
    )
    text = raw.decode("utf-8", errors="ignore")
    assert "From: me@example.com" in text
    assert "To: jane@example.com" in text
    assert "Subject: Re: invoice" in text
    assert "In-Reply-To: <orig@mail.example.com>" in text
    assert "References: <orig@mail.example.com>" in text


def test_build_rfc822_omits_threading_headers_for_new_message() -> None:
    raw = _build_rfc822(
        sender="me@example.com", to=["jane@example.com"], subject="hi", body="hello"
    )
    text = raw.decode("utf-8", errors="ignore")
    assert "In-Reply-To" not in text
    assert "References" not in text


# ---------------------------------------------------------------------------
# Service: API methods (mocked _request)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_search_messages_fetches_summaries_per_id() -> None:
    service = _make_service()
    list_resp = {"messages": [{"id": "m1"}, {"id": "m2"}]}
    detail_m1 = {
        "id": "m1",
        "threadId": "t1",
        "snippet": "hello",
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Hi"},
                {"name": "Date", "value": "Tue, 01 Jan 2026 12:00:00 +0000"},
            ]
        },
    }
    detail_m2 = {
        "id": "m2",
        "threadId": "t2",
        "snippet": "world",
        "payload": {
            "headers": [
                {"name": "From", "value": "bob@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Tue, 01 Jan 2026 13:00:00 +0000"},
            ]
        },
    }

    responses: list[Any] = [list_resp, detail_m1, detail_m2]

    async def fake_request(_method: str, _path: str, **_kwargs: Any) -> Any:
        return responses.pop(0)

    with patch.object(service, "_request", side_effect=fake_request):
        results = await service.search_messages("from:alice", max_results=10)

    assert [r.id for r in results] == ["m1", "m2"]
    assert results[0].sender == "alice@example.com"
    assert results[0].subject == "Hi"


@pytest.mark.asyncio()
async def test_search_messages_skips_404_per_id() -> None:
    service = _make_service()
    list_resp = {"messages": [{"id": "m1"}, {"id": "m2"}]}
    detail_m2 = {
        "id": "m2",
        "threadId": "t2",
        "snippet": "ok",
        "payload": {
            "headers": [
                {"name": "From", "value": "bob@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": ""},
            ]
        },
    }
    not_found = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(404),
    )

    responses: list[Any] = [list_resp, not_found, detail_m2]

    async def fake_request(_method: str, _path: str, **_kwargs: Any) -> Any:
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch.object(service, "_request", side_effect=fake_request):
        results = await service.search_messages("anything", max_results=10)

    assert [r.id for r in results] == ["m2"]


@pytest.mark.asyncio()
async def test_search_messages_caps_max_results() -> None:
    service = _make_service()
    captured: dict[str, dict[str, str]] = {}

    async def fake_request(
        _method: str, _path: str, *, params: dict[str, str] | None = None, **_kwargs: Any
    ) -> Any:
        captured["params"] = params or {}
        return {"messages": []}

    with patch.object(service, "_request", side_effect=fake_request):
        await service.search_messages("q", max_results=10000)

    # Internal ceiling is 500.
    assert captured["params"]["maxResults"] == "500"


@pytest.mark.asyncio()
async def test_get_message_returns_full_message_with_links() -> None:
    service = _make_service()
    body_text = "Hi there\nHere is a link https://magic.example/?token=abc\nThanks"
    api_resp = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "To", "value": "me@example.com, you@example.com"},
                {"name": "Cc", "value": "cc@example.com"},
                {"name": "Subject", "value": "Magic link"},
                {"name": "Date", "value": "Tue, 01 Jan 2026 12:00:00 +0000"},
                {"name": "Message-ID", "value": "<orig@mail.example.com>"},
            ],
        },
    }

    with patch.object(service, "_request", new_callable=AsyncMock, return_value=api_resp):
        msg = await service.get_message("m1")

    assert msg.id == "m1"
    assert msg.sender == "alice@example.com"
    assert msg.recipients == ["me@example.com", "you@example.com"]
    assert msg.cc == ["cc@example.com"]
    assert msg.subject == "Magic link"
    assert "Hi there" in msg.body
    assert "https://magic.example/?token=abc" in msg.links
    assert msg.rfc822_message_id == "<orig@mail.example.com>"


@pytest.mark.asyncio()
async def test_send_message_threads_when_reply_id_given() -> None:
    service = _make_service()
    parent = GmailMessage(
        id="parent",
        thread_id="thread-1",
        sender="alice@example.com",
        recipients=["me@example.com"],
        cc=[],
        subject="Original",
        date="",
        body="hi",
        rfc822_message_id="<orig@mail.example.com>",
    )

    with (
        patch.object(service, "get_message", new_callable=AsyncMock, return_value=parent),
        patch.object(service, "_request", new_callable=AsyncMock) as mock_req,
    ):
        mock_req.return_value = {"id": "sent-1", "threadId": "thread-1"}
        result = await service.send_message(
            to=["alice@example.com"],
            subject="Re: Original",
            body="thanks",
            reply_to_message_id="parent",
        )

    assert isinstance(result, GmailSendResult)
    assert result.thread_id == "thread-1"
    # Inspect the send body
    args, kwargs = mock_req.call_args
    assert args[0] == "POST"
    assert args[1] == "/users/me/messages/send"
    body = kwargs["json"]
    assert body["threadId"] == "thread-1"
    raw_decoded = base64.urlsafe_b64decode(body["raw"]).decode("utf-8", errors="ignore")
    assert "In-Reply-To: <orig@mail.example.com>" in raw_decoded
    assert "References: <orig@mail.example.com>" in raw_decoded


@pytest.mark.asyncio()
async def test_send_message_omits_threadid_for_new_message() -> None:
    service = _make_service()
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"id": "sent-1", "threadId": "thread-x"}
        await service.send_message(to=["alice@example.com"], subject="hi", body="hello")
    body = mock_req.call_args.kwargs["json"]
    assert "threadId" not in body


@pytest.mark.asyncio()
async def test_send_message_requires_recipient() -> None:
    service = _make_service()
    with pytest.raises(ValueError, match="recipient"):
        await service.send_message(to=[], subject="x", body="y")


@pytest.mark.asyncio()
async def test_request_refreshes_and_retries_on_401() -> None:
    """A 401 from Gmail triggers a refresh+retry so a stale access token gets rotated."""
    service = _make_service()

    response_401 = MagicMock(status_code=401, content=b"")
    response_401.raise_for_status = MagicMock()
    response_200 = MagicMock(status_code=200, content=b'{"ok": true}')
    response_200.json.return_value = {"ok": True}
    response_200.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.request = AsyncMock(side_effect=[response_401, response_200])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    def rotate_token(_client: httpx.AsyncClient) -> None:
        service._access_token = "rotated-token"

    with (
        patch(
            "backend.app.integrations.gmail.service.httpx.AsyncClient",
            return_value=fake_client,
        ),
        patch.object(
            service, "_refresh_access_token", new=AsyncMock(side_effect=rotate_token)
        ) as mock_refresh,
    ):
        result = await service._request("GET", "/users/me/profile")

    assert result == {"ok": True}
    mock_refresh.assert_awaited_once()
    assert fake_client.request.await_count == 2
    second_headers = fake_client.request.await_args_list[1].kwargs["headers"]
    assert second_headers["Authorization"] == "Bearer rotated-token"


@pytest.mark.asyncio()
async def test_send_message_resolves_sender_lazily() -> None:
    service = GmailService(
        access_token="t",
        refresh_token="r",
        client_id="cid",
        client_secret="csec",
    )
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        # First call: get_profile resolution. Second call: send.
        mock_req.side_effect = [
            {"emailAddress": "me@example.com"},
            {"id": "sent", "threadId": "t"},
        ]
        await service.send_message(to=["a@x.com"], subject="s", body="b")
    # First request should be /users/me/profile
    first_args, _ = mock_req.call_args_list[0]
    assert first_args == ("GET", "/users/me/profile")


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def gmail_tools() -> list[Tool]:
    service = _make_service()
    return create_gmail_tools(service)


def test_create_gmail_tools_returns_4_tools(gmail_tools: list[Tool]) -> None:
    assert len(gmail_tools) == 4


def test_gmail_tool_names(gmail_tools: list[Tool]) -> None:
    names = {t.name for t in gmail_tools}
    assert names == {
        ToolName.GMAIL_SEARCH,
        ToolName.GMAIL_GET_MESSAGE,
        ToolName.GMAIL_LIST_RECENT,
        ToolName.GMAIL_SEND,
    }


def test_gmail_tools_have_params_model(gmail_tools: list[Tool]) -> None:
    for tool in gmail_tools:
        assert tool.params_model is not None, f"Tool {tool.name} missing params_model"


def test_all_gmail_tools_default_to_ask(gmail_tools: list[Tool]) -> None:
    """User asked for ask-before-read AND ask-before-send."""
    for tool in gmail_tools:
        assert tool.approval_policy is not None, tool.name
        assert tool.approval_policy.default_level == PermissionLevel.ASK, tool.name


def test_send_description_distinguishes_reply_from_new(gmail_tools: list[Tool]) -> None:
    tool = _get_tool(gmail_tools, ToolName.GMAIL_SEND)
    assert tool.approval_policy is not None
    assert tool.approval_policy.description_builder is not None
    new_desc = tool.approval_policy.description_builder(
        {"to": ["alice@example.com"], "subject": "hi"}
    )
    assert "alice@example.com" in new_desc
    reply_desc = tool.approval_policy.description_builder(
        {"to": ["alice@example.com"], "reply_to_message_id": "m1"}
    )
    assert "Reply" in reply_desc


# ---------------------------------------------------------------------------
# Tool behaviour (success and error paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_gmail_search_tool_formats_results() -> None:
    service = _make_service()
    summaries = [
        GmailMessageSummary(
            id="m1",
            thread_id="t1",
            sender="alice@example.com",
            subject="Hi",
            date="Tue, 01 Jan 2026 12:00:00 +0000",
            snippet="snippet text",
        )
    ]
    with patch.object(service, "search_messages", new_callable=AsyncMock, return_value=summaries):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_SEARCH)
        result = await tool.function("from:alice", 5)

    assert result.is_error is False
    assert "Found 1 message(s)" in result.content
    assert "alice@example.com" in result.content
    assert "[id: m1]" in result.content


@pytest.mark.asyncio()
async def test_gmail_search_tool_handles_401_as_disconnected() -> None:
    service = _make_service()
    err = httpx.HTTPStatusError(
        "401",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(401),
    )
    with patch.object(service, "search_messages", new_callable=AsyncMock, side_effect=err):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_SEARCH)
        result = await tool.function("q", 5)

    assert result.is_error is True
    # AUTH (not SERVICE) so the LLM hint tells the user to reauthenticate
    # via the dashboard instead of suggesting a transient retry.
    assert result.error_kind == ToolErrorKind.AUTH
    assert "disconnected" in result.content.lower()


@pytest.mark.asyncio()
async def test_gmail_search_tool_403_surfaces_gmail_message() -> None:
    """403 with a non-scope cause (Gmail API disabled in GCP) must surface the
    actual Gmail message, not the canned 'missing scope, reconnect' guess.

    Regression for the production incident where a real ``accessNotConfigured``
    error was rendered as "missing the required scope; disconnect and reconnect"
    AND tagged ``error_kind=VALIDATION``, which made the agent append the
    misleading "[Check the expected parameter format...]" hint.
    """
    service = _make_service()
    # The message field is the exact text Gmail returned in prod for this
    # bug. The full sentence ("If you enabled this API recently, wait a few
    # minutes for the action to propagate...") must round-trip untruncated,
    # because that's the actionable hint right after the operator hits Enable.
    gmail_message = (
        "Gmail API has not been used in project 1033137896781 before or it is "
        "disabled. Enable it by visiting https://console.developers.google.com/"
        "apis/api/gmail.googleapis.com/overview?project=1033137896781 then "
        "retry. If you enabled this API recently, wait a few minutes for the "
        "action to propagate to our systems and retry."
    )
    body = json.dumps(
        {
            "error": {
                "code": 403,
                "message": gmail_message,
                "errors": [
                    {
                        "reason": "accessNotConfigured",
                        "message": "Gmail API has not been used in project...",
                    }
                ],
            }
        }
    )
    err = httpx.HTTPStatusError(
        "403",
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
        response=httpx.Response(403, text=body),
    )
    with patch.object(service, "search_messages", new_callable=AsyncMock, side_effect=err):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_SEARCH)
        result = await tool.function("q", 5)

    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.PERMISSION
    # Full message round-trips, including the trailing "wait a few minutes"
    # sentence that the previous 300-char cap dropped.
    assert gmail_message in result.content
    assert "wait a few minutes" in result.content
    assert "..." not in result.content
    # The canned scope-reconnect guess MUST NOT appear for this 403.
    assert "missing" not in result.content.lower()
    assert "reconnect" not in result.content.lower()


@pytest.mark.asyncio()
async def test_gmail_search_tool_403_insufficient_permissions_keeps_reconnect_hint() -> None:
    """When Gmail's reason is genuinely ``insufficientPermissions`` the
    "disconnect and reconnect to grant scopes" advice IS the right fix, so we
    keep it for that specific reason."""
    service = _make_service()
    body = (
        '{"error": {"code": 403, "message": "Insufficient Permission", '
        '"errors": [{"reason": "insufficientPermissions", '
        '"message": "Insufficient Permission"}]}}'
    )
    err = httpx.HTTPStatusError(
        "403",
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
        response=httpx.Response(403, text=body),
    )
    with patch.object(service, "search_messages", new_callable=AsyncMock, side_effect=err):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_SEARCH)
        result = await tool.function("q", 5)

    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.PERMISSION
    assert "missing a required scope" in result.content
    assert "reconnect" in result.content.lower()


@pytest.mark.asyncio()
async def test_gmail_search_tool_403_with_unparseable_body_falls_back() -> None:
    service = _make_service()
    err = httpx.HTTPStatusError(
        "403",
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
        response=httpx.Response(403, text="<html>upstream proxy noise</html>"),
    )
    with patch.object(service, "search_messages", new_callable=AsyncMock, side_effect=err):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_SEARCH)
        result = await tool.function("q", 5)

    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.PERMISSION
    assert "HTTP 403" in result.content


@pytest.mark.asyncio()
async def test_gmail_get_message_tool_renders_full_message() -> None:
    service = _make_service()
    msg = GmailMessage(
        id="m1",
        thread_id="t1",
        sender="alice@example.com",
        recipients=["me@example.com"],
        cc=[],
        subject="Magic Link",
        date="Tue",
        body="Click https://magic.example/?token=abc",
        links=["https://magic.example/?token=abc"],
        rfc822_message_id="<orig@x>",
    )
    with patch.object(service, "get_message", new_callable=AsyncMock, return_value=msg):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_GET_MESSAGE)
        result = await tool.function("m1")

    assert result.is_error is False
    assert "Subject: Magic Link" in result.content
    assert "https://magic.example/?token=abc" in result.content


@pytest.mark.asyncio()
async def test_gmail_send_tool_emits_receipt() -> None:
    service = _make_service()
    sent = GmailSendResult(id="sent-1", thread_id="thread-1")
    with patch.object(service, "send_message", new_callable=AsyncMock, return_value=sent):
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_SEND)
        result = await tool.function(["alice@example.com"], "subj", "body")

    assert result.is_error is False
    assert result.receipt is not None
    assert result.receipt.action == "Sent email via Gmail"
    assert "alice@example.com" in result.receipt.target


@pytest.mark.asyncio()
async def test_gmail_send_tool_rejects_empty_recipients() -> None:
    service = _make_service()
    tools = create_gmail_tools(service)
    tool = _get_tool(tools, ToolName.GMAIL_SEND)
    result = await tool.function([], "s", "b")
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


@pytest.mark.asyncio()
async def test_gmail_list_recent_calls_search_with_empty_query() -> None:
    service = _make_service()
    with patch.object(service, "search_messages", new_callable=AsyncMock, return_value=[]) as m:
        tools = create_gmail_tools(service)
        tool = _get_tool(tools, ToolName.GMAIL_LIST_RECENT)
        result = await tool.function(7)
    m.assert_awaited_once_with("", 7)
    # Empty inbox should render a friendly message, not the search-style fallback.
    assert result.content == "Inbox is empty."


# ---------------------------------------------------------------------------
# Factory + auth_check
# ---------------------------------------------------------------------------


def _make_ctx() -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = "1"
    ctx.user = user
    return ctx


@pytest.mark.asyncio()
async def test_gmail_factory_returns_empty_when_not_configured() -> None:
    with patch("backend.app.integrations.gmail.factory.settings") as mock_settings:
        mock_settings.gmail_client_id = ""
        mock_settings.gmail_client_secret = ""
        assert await _gmail_factory(_make_ctx()) == []


@pytest.mark.asyncio()
async def test_gmail_factory_returns_empty_when_user_not_connected() -> None:
    with (
        patch("backend.app.integrations.gmail.factory.settings") as mock_settings,
        patch("backend.app.integrations.gmail.factory.oauth_service") as mock_oauth,
    ):
        mock_settings.gmail_client_id = "cid"
        mock_settings.gmail_client_secret = "csec"
        mock_oauth.get_valid_token = AsyncMock(return_value=None)
        assert await _gmail_factory(_make_ctx()) == []


@pytest.mark.asyncio()
async def test_gmail_factory_returns_4_tools_when_connected() -> None:
    token = MagicMock()
    token.access_token = "ax"
    token.refresh_token = "rx"
    token.expires_at = 9999999999.0
    with (
        patch("backend.app.integrations.gmail.factory.settings") as mock_settings,
        patch("backend.app.integrations.gmail.factory.oauth_service") as mock_oauth,
    ):
        mock_settings.gmail_client_id = "cid"
        mock_settings.gmail_client_secret = "csec"
        mock_oauth.get_valid_token = AsyncMock(return_value=token)
        tools = await _gmail_factory(_make_ctx())
    assert len(tools) == 4


@pytest.mark.asyncio()
async def test_gmail_auth_check_returns_none_when_unconfigured() -> None:
    """When the operator has not configured Gmail, hide the integration entirely."""
    with patch("backend.app.integrations.gmail.factory.settings") as mock_settings:
        mock_settings.gmail_client_id = ""
        mock_settings.gmail_client_secret = ""
        assert await _gmail_auth_check(_make_ctx()) is None


@pytest.mark.asyncio()
async def test_gmail_auth_check_returns_reason_when_user_not_connected() -> None:
    """When admin configured Gmail but user hasn't, surface a reason string."""
    with (
        patch("backend.app.integrations.gmail.factory.settings") as mock_settings,
        patch("backend.app.integrations.gmail.factory.oauth_service") as mock_oauth,
    ):
        mock_settings.gmail_client_id = "cid"
        mock_settings.gmail_client_secret = "csec"
        mock_oauth.load_token = AsyncMock(return_value=None)
        reason = await _gmail_auth_check(_make_ctx())
    assert reason is not None
    assert "Gmail is not connected" in reason
    assert "manage_integration" in reason


@pytest.mark.asyncio()
async def test_gmail_auth_check_returns_none_when_user_connected() -> None:
    token = MagicMock()
    token.access_token = "ax"
    with (
        patch("backend.app.integrations.gmail.factory.settings") as mock_settings,
        patch("backend.app.integrations.gmail.factory.oauth_service") as mock_oauth,
    ):
        mock_settings.gmail_client_id = "cid"
        mock_settings.gmail_client_secret = "csec"
        mock_oauth.load_token = AsyncMock(return_value=token)
        assert await _gmail_auth_check(_make_ctx()) is None


# ---------------------------------------------------------------------------
# manage_integration discovery
# ---------------------------------------------------------------------------


def test_manage_integration_includes_gmail_in_display_names() -> None:
    """The Gmail factory must declare its display name on the registry.

    After #1260, integration_tools no longer keeps a hand-maintained name
    dict; display metadata lives on each ToolFactory. The Gmail factory
    name and OAuth name are both ``gmail``, so no oauth_name override is
    needed.
    """
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()

    factory = default_registry.get_factory("gmail")
    assert factory is not None
    assert factory.display_name == "Gmail"


def test_manage_integration_hint_mentions_gmail() -> None:
    """The system-prompt hint built from the integration registries should list Gmail."""
    from backend.app.agent.tools.integration_tools import _build_available_integrations_hint
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()

    hint = _build_available_integrations_hint(default_registry)
    assert "Gmail" in hint
    assert "'gmail'" in hint
