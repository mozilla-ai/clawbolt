"""Tests for ``QuickBooksOnlineService`` HTTP behavior.

Focus on idempotency: every POST that mutates state (create, update, send)
must include a ``requestid`` query parameter so QBO collapses retries to a
single entity. The 401-refresh retry path inside ``_request`` must reuse
the same ``requestid``, otherwise a transient auth blip would create
duplicates.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from backend.app.integrations.quickbooks import service as service_module
from backend.app.integrations.quickbooks.service import QuickBooksOnlineService


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Route every ``httpx.AsyncClient`` constructed inside the service to
    ``handler`` via ``MockTransport``."""
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module.httpx, "AsyncClient", factory)


def _service() -> QuickBooksOnlineService:
    return QuickBooksOnlineService(
        client_id="cid",
        client_secret="csec",
        realm_id="9999",
        access_token="initial-access",
        refresh_token="rfresh",
        environment="production",
        token_url="https://example.invalid/token",
    )


@pytest.mark.asyncio()
async def test_create_entity_includes_requestid_in_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Estimate": {"Id": "1", "TotalAmt": 10.0}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.create_entity("Estimate", {"CustomerRef": {"value": "1"}, "Line": []})

    assert len(captured) == 1
    rid = captured[0].url.params.get("requestid")
    assert rid is not None
    assert len(rid) >= 16


@pytest.mark.asyncio()
async def test_update_entity_includes_requestid(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Estimate": {"Id": "1", "SyncToken": "1"}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.update_entity("Estimate", {"Id": "1", "SyncToken": "0"})

    assert captured[0].url.params.get("requestid")


@pytest.mark.asyncio()
async def test_send_entity_email_includes_requestid_alongside_sendTo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Estimate": {"Id": "1", "EmailStatus": "EmailSent"}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.send_entity_email("Estimate", "1", "to@example.com")

    params = captured[0].url.params
    assert params.get("requestid")
    assert params.get("sendTo") == "to@example.com"


@pytest.mark.asyncio()
async def test_send_entity_email_uses_octet_stream_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intuit's /send endpoint requires application/octet-stream and 500s
    on application/json. Every other endpoint stays on application/json."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Estimate": {"Id": "1", "EmailStatus": "EmailSent"}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.send_entity_email("Estimate", "1", "to@example.com")

    assert captured[0].headers.get("content-type") == "application/octet-stream"
    # /send takes its recipient via query param, not body.
    assert captured[0].content == b""


@pytest.mark.asyncio()
async def test_create_entity_uses_json_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Estimate": {"Id": "1", "TotalAmt": 10.0}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.create_entity("Estimate", {"CustomerRef": {"value": "1"}, "Line": []})

    assert captured[0].headers.get("content-type") == "application/json"


@pytest.mark.asyncio()
async def test_query_uses_json_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"QueryResponse": {"Estimate": []}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.query("SELECT * FROM Estimate")

    assert captured[0].headers.get("content-type") == "application/json"


@pytest.mark.asyncio()
async def test_query_does_not_set_requestid(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"QueryResponse": {"Estimate": []}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.query("SELECT * FROM Estimate")

    assert "requestid" not in captured[0].url.params


@pytest.mark.asyncio()
async def test_401_retry_reuses_same_requestid(monkeypatch: pytest.MonkeyPatch) -> None:
    """First POST returns 401, refresh succeeds, retry POST must carry the
    same ``requestid`` so QBO collapses the pair to a single entity."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-rfresh",
                    "expires_in": 3600,
                },
            )
        if request.method == "POST" and "/estimate" in request.url.path:
            posts = [r for r in captured if r.method == "POST" and "/estimate" in r.url.path]
            if len(posts) == 1:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(200, json={"Estimate": {"Id": "1", "TotalAmt": 10.0}})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    svc = _service()
    result = await svc.create_entity("Estimate", {"CustomerRef": {"value": "1"}, "Line": []})

    posts = [r for r in captured if r.method == "POST" and "/estimate" in r.url.path]
    assert len(posts) == 2, "expected initial POST + retry after token refresh"
    rid_first = posts[0].url.params.get("requestid")
    rid_retry = posts[1].url.params.get("requestid")
    assert rid_first is not None
    assert rid_first == rid_retry, "401 retry must reuse the requestid so QBO dedupes the pair"
    assert result["Id"] == "1"


@pytest.mark.asyncio()
async def test_each_create_call_uses_a_fresh_requestid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two distinct logical creates must use distinct requestids; otherwise
    QBO would dedupe the second one as a duplicate of the first."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Estimate": {"Id": "1", "TotalAmt": 10.0}})

    _patch_transport(monkeypatch, handler)
    svc = _service()
    await svc.create_entity("Estimate", {"CustomerRef": {"value": "1"}, "Line": []})
    await svc.create_entity("Estimate", {"CustomerRef": {"value": "2"}, "Line": []})

    rid1 = captured[0].url.params.get("requestid")
    rid2 = captured[1].url.params.get("requestid")
    assert rid1 and rid2
    assert rid1 != rid2


@pytest.mark.asyncio()
async def test_send_entity_email_validates_inputs_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad inputs should raise without ever hitting the network."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(500)

    _patch_transport(monkeypatch, handler)
    svc = _service()
    with pytest.raises(ValueError):
        await svc.send_entity_email("Estimate", "abc", "to@example.com")
    with pytest.raises(ValueError):
        await svc.send_entity_email("Estimate", "1", "not-an-email")
    assert captured == []
