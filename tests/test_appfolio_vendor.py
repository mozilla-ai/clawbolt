"""Tests for the AppFolio Vendor Portal integration."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.integrations.appfolio_vendor.auth import (
    AppFolioCredential,
    MagicLinkError,
    extract_magic_link_token,
    generate_fingerprint,
    is_connected,
    load_credential,
    save_credential,
    upsert_fingerprint,
)
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioConnectParams,
    AppFolioListWorkOrdersParams,
)
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    AppFolioVendorService,
    AuthExpiredError,
    AuthScopeError,
    build_service,
    exchange_magic_link,
)

# ---------------------------------------------------------------------------
# Magic-link parsing
# ---------------------------------------------------------------------------


def test_extract_magic_link_from_full_url() -> None:
    url = "https://vendor.appfolio.com/?magic_link_token=eyJabc.def&other=x"
    assert extract_magic_link_token(url) == "eyJabc.def"


def test_extract_magic_link_from_appf_io_host() -> None:
    url = "https://vendor.appf.io/?magic_link_token=tok123"
    assert extract_magic_link_token(url) == "tok123"


def test_extract_magic_link_from_query_fragment() -> None:
    assert extract_magic_link_token("?magic_link_token=abc123&foo=1") == "abc123"


def test_extract_magic_link_from_bare_token() -> None:
    assert extract_magic_link_token("eyJpartA.partB.partC") == "eyJpartA.partB.partC"


def test_extract_magic_link_strips_whitespace() -> None:
    assert extract_magic_link_token("  https://x/?magic_link_token=t  ") == "t"


def test_extract_magic_link_rejects_empty() -> None:
    with pytest.raises(MagicLinkError):
        extract_magic_link_token("")


def test_extract_magic_link_rejects_unparseable() -> None:
    with pytest.raises(MagicLinkError):
        extract_magic_link_token("foo=bar")


def test_extract_magic_link_url_without_token_param() -> None:
    with pytest.raises(MagicLinkError):
        extract_magic_link_token("https://vendor.appfolio.com/?other=x")


# ---------------------------------------------------------------------------
# Fingerprint generation
# ---------------------------------------------------------------------------


def test_generate_fingerprint_is_hex_and_unique() -> None:
    a = generate_fingerprint()
    b = generate_fingerprint()
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)
    assert a != b


# ---------------------------------------------------------------------------
# Credential persistence (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_credential_save_and_load_round_trip(async_test_user: Any) -> None:
    user_id = async_test_user.id

    assert await is_connected(user_id) is False
    assert await load_credential(user_id) is None

    await save_credential(
        user_id=user_id,
        jwt="eyJ.fake.jwt",
        fingerprint="aabbcc",
        customer_ids=["cust1", "cust2"],
        extra_metadata={"shape": "round"},
    )

    cred = await load_credential(user_id)
    assert cred is not None
    assert cred.jwt == "eyJ.fake.jwt"
    assert cred.fingerprint == "aabbcc"
    assert cred.customer_ids == ["cust1", "cust2"]
    assert cred.extra["shape"] == "round"
    assert await is_connected(user_id) is True


@pytest.mark.asyncio()
async def test_upsert_fingerprint_persists_and_reuses(async_test_user: Any) -> None:
    user_id = async_test_user.id
    first = await upsert_fingerprint(user_id)
    second = await upsert_fingerprint(user_id)
    assert first == second
    assert len(first) == 32

    # Subsequent save_credential should not clobber the fingerprint when
    # the caller passes the same value.
    await save_credential(
        user_id=user_id,
        jwt="jwt-1",
        fingerprint=first,
        customer_ids=["c1"],
    )
    cred = await load_credential(user_id)
    assert cred is not None
    assert cred.fingerprint == first


@pytest.mark.asyncio()
async def test_load_credential_without_jwt_returns_none(async_test_user: Any) -> None:
    """A row created by upsert_fingerprint alone should not look connected."""
    user_id = async_test_user.id
    await upsert_fingerprint(user_id)
    assert await load_credential(user_id) is None
    assert await is_connected(user_id) is False


@pytest.mark.asyncio()
async def test_save_credential_writes_refresh_token_to_encrypted_column(
    async_test_user: Any,
) -> None:
    """Refresh token must land in the dedicated encrypted column, not extra_json."""
    import json as _json

    import sqlalchemy as sa

    from backend.app.database import db_session_async
    from backend.app.integrations.appfolio_vendor.auth import INTEGRATION_NAME
    from backend.app.models import OAuthToken

    user_id = async_test_user.id
    await save_credential(
        user_id=user_id,
        jwt="jwt-abc",
        fingerprint="fp-1",
        customer_ids=["c1"],
        refresh_token="refresh-secret",
    )

    async with db_session_async() as session:
        row = (
            await session.execute(
                sa.select(OAuthToken).where(
                    OAuthToken.user_id == user_id,
                    OAuthToken.integration == INTEGRATION_NAME,
                )
            )
        ).scalar_one()
        extra = _json.loads(row.extra_json)

    assert row.refresh_token == "refresh-secret"
    assert "refresh_token" not in extra, "refresh token must not leak into plaintext extra_json"

    cred = await load_credential(user_id)
    assert cred is not None
    assert cred.refresh_token == "refresh-secret"


@pytest.mark.asyncio()
async def test_load_credential_falls_back_to_legacy_extra_refresh_token(
    async_test_user: Any,
) -> None:
    """Pre-fix rows store refresh_token in extra_json; load_credential must still find it."""
    import json as _json
    from datetime import UTC, datetime

    from backend.app.database import db_session_async
    from backend.app.integrations.appfolio_vendor.auth import INTEGRATION_NAME
    from backend.app.models import OAuthToken

    user_id = async_test_user.id
    now = datetime.now(UTC)
    async with db_session_async() as session:
        session.add(
            OAuthToken(
                user_id=user_id,
                integration=INTEGRATION_NAME,
                access_token="legacy-jwt",
                refresh_token="",  # legacy rows left this empty
                token_type="Bearer",
                extra_json=_json.dumps(
                    {
                        "fingerprint": "fp-legacy",
                        "customer_ids": ["c1"],
                        "refresh_token": "legacy-refresh",
                    }
                ),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    cred = await load_credential(user_id)
    assert cred is not None
    assert cred.refresh_token == "legacy-refresh"


@pytest.mark.asyncio()
async def test_save_credential_strips_legacy_refresh_token_from_extra(
    async_test_user: Any,
) -> None:
    """A subsequent save must wipe a legacy plaintext refresh_token left in extra_json."""
    import json as _json
    from datetime import UTC, datetime

    import sqlalchemy as sa

    from backend.app.database import db_session_async
    from backend.app.integrations.appfolio_vendor.auth import INTEGRATION_NAME
    from backend.app.models import OAuthToken

    user_id = async_test_user.id
    now = datetime.now(UTC)
    async with db_session_async() as session:
        session.add(
            OAuthToken(
                user_id=user_id,
                integration=INTEGRATION_NAME,
                access_token="legacy-jwt",
                refresh_token="",
                token_type="Bearer",
                extra_json=_json.dumps(
                    {
                        "fingerprint": "fp-legacy",
                        "customer_ids": [],
                        "refresh_token": "old-plaintext",
                    }
                ),
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    await save_credential(
        user_id=user_id,
        jwt="new-jwt",
        fingerprint="fp-legacy",
        customer_ids=[],
        refresh_token="new-encrypted",
    )

    async with db_session_async() as session:
        row = (
            await session.execute(
                sa.select(OAuthToken).where(
                    OAuthToken.user_id == user_id,
                    OAuthToken.integration == INTEGRATION_NAME,
                )
            )
        ).scalar_one()
        extra = _json.loads(row.extra_json)

    assert row.refresh_token == "new-encrypted"
    assert "refresh_token" not in extra


# ---------------------------------------------------------------------------
# Service HTTP layer
# ---------------------------------------------------------------------------


def _credential() -> AppFolioCredential:
    return AppFolioCredential(
        user_id="u1",
        jwt="jwt-1",
        fingerprint="fp-1",
        customer_ids=["c1"],
        extra={"fingerprint": "fp-1", "customer_ids": ["c1"]},
    )


def _mock_response(
    json_data: Any = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    text: str = "stub error body",
) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {"content-type": "application/json"}
    resp.content = b"{}" if json_data is None else b'{"x": 1}'
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


def _patch_async_client(method: str, response: httpx.Response) -> Any:
    """Patch httpx.AsyncClient so a single request returns ``response``."""
    client = AsyncMock()
    setattr(client, method, AsyncMock(return_value=response))
    client.request = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_build_service_validates_credential() -> None:
    bad = AppFolioCredential(user_id="u", jwt="", fingerprint="fp", customer_ids=[], extra={})
    with pytest.raises(ValueError, match="no JWT"):
        build_service(bad, api_base="https://x")
    no_fp = AppFolioCredential(user_id="u", jwt="j", fingerprint="", customer_ids=[], extra={})
    with pytest.raises(ValueError, match="no fingerprint"):
        build_service(no_fp, api_base="https://x")


def test_service_headers_include_required_set() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    headers = service._headers()
    assert headers["Authorization"] == "Bearer jwt-1"
    assert headers["X-Fingerprint"] == "fp-1"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Vendor-Portal-Web-Client"]


def test_service_full_url_handles_relative_and_absolute() -> None:
    s = AppFolioVendorService(_credential(), api_base="https://api.test")
    assert s._full_url("/foo") == "https://api.test/foo"
    assert s._full_url("foo") == "https://api.test/foo"
    assert s._full_url("https://other/x") == "https://other/x"


@pytest.mark.asyncio()
async def test_service_get_returns_json() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"hello": "world"})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        result = await service.get("/ping")
    assert result == {"hello": "world"}


@pytest.mark.asyncio()
async def test_service_401_with_login_url_raises_auth_expired() -> None:
    """401 + ``login_url`` body means the JWT actually expired.

    AppFolio's contract: the SPA sees this and redirects the user to
    re-auth. We mirror that signal as :class:`AuthExpiredError` so
    tools can prompt the user for a fresh magic link.
    """
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"login_url": "https://login/here"}, status_code=401)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        with pytest.raises(AuthExpiredError) as exc_info:
            await service.get("/anything")
    assert exc_info.value.login_url == "https://login/here"


@pytest.mark.asyncio()
async def test_service_401_without_login_url_raises_auth_scope() -> None:
    """401 without a ``login_url`` body means the request scope is wrong.

    The JWT itself is fine; the request's ``customer_id`` (in the path
    or the body) is not in this credential's authorized set.
    Reconnecting will not help, so we must NOT raise
    :class:`AuthExpiredError` and tell the user to log in again.
    """
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={}, status_code=401)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        with pytest.raises(AuthScopeError) as exc_info:
            await service.get("/anything")
    # AuthScopeError must NOT be a misclassified AuthExpiredError; the
    # tool layer checks isinstance() and would tell the user to
    # reconnect (the trust-eroding bug we're fixing).
    assert not isinstance(exc_info.value, AuthExpiredError)


@pytest.mark.asyncio()
async def test_service_error_to_tool_result_distinguishes_scope_from_expired() -> None:
    """The errors→ToolResult mapper must give scope and expired
    different user-facing messages and hints.
    """
    from backend.app.integrations.appfolio_vendor.errors import (
        service_error_to_tool_result,
    )

    expired = service_error_to_tool_result(
        "creating invoice", AuthExpiredError(login_url="https://login/x")
    )
    scope = service_error_to_tool_result("creating invoice", AuthScopeError("scope mismatch"))

    assert expired.is_error and scope.is_error
    # Expired should mention the magic-link reconnect hint.
    assert "magic link" in (expired.hint or "")
    # Scope must NOT tell the user to reconnect; that erodes trust
    # when the next reconnect produces the same 401.
    assert "magic link" not in (scope.hint or "")
    assert "reconnect" not in (scope.content or "").lower()


def test_log_unexpected_response_shape_dict(caplog: Any) -> None:
    """The helper logs a structured WARNING with sorted keys and a body preview."""
    import logging

    from backend.app.integrations.appfolio_vendor.errors import (
        log_unexpected_response_shape,
    )

    payload = {"unexpected_field": "value", "other": [1, 2]}
    with caplog.at_level(logging.WARNING, logger="backend.app.integrations.appfolio_vendor.errors"):
        log_unexpected_response_shape("test_tool", payload, expected="dict with `data` key")
    assert any(
        "test_tool" in r.message
        and "dict with `data` key" in r.message
        and "['other', 'unexpected_field']" in r.message
        and "unexpected_field" in r.message
        for r in caplog.records
    )


def test_log_unexpected_response_shape_list(caplog: Any) -> None:
    """List payloads log length plus the first item's keys when that item is a dict."""
    import logging

    from backend.app.integrations.appfolio_vendor.errors import (
        log_unexpected_response_shape,
    )

    payload = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    with caplog.at_level(logging.WARNING, logger="backend.app.integrations.appfolio_vendor.errors"):
        log_unexpected_response_shape("test_tool", payload, expected="dict envelope")
    assert any("list len=2" in r.message and "['id', 'name']" in r.message for r in caplog.records)


@pytest.mark.asyncio()
async def test_service_5xx_raises_appfolio_error() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(status_code=503)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        with pytest.raises(AppFolioError):
            await service.get("/x")


def _noisy_jpeg(width: int, height: int, quality: int = 98) -> bytes:
    """Build a noise JPEG of approximately ``width x height`` at given quality."""
    import io
    import os

    from PIL import Image

    data = os.urandom(width * height * 3)
    img = Image.frombytes("RGB", (width, height), data)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def test_encode_files_compresses_oversized_photos() -> None:
    """Photos above the AppFolio target should be compressed before encoding.

    Six full-resolution phone photos easily exceed the upload timeout
    when inlined as base64; we shrink each one to a documentary-quality
    JPEG before send.
    """
    from backend.app.integrations.appfolio_vendor.service import (
        _APPFOLIO_PHOTO_TARGET_BYTES,
        FileUpload,
        _encode_files,
    )

    big = _noisy_jpeg(4000, 3000)
    assert len(big) > _APPFOLIO_PHOTO_TARGET_BYTES, "test setup: need oversized image"

    encoded = _encode_files([FileUpload(name="damage.jpg", data=big)])
    assert len(encoded) == 1
    assert encoded[0]["name"] == "damage.jpg"
    raw = base64.b64decode(encoded[0]["file_in_base64"])
    assert len(raw) <= _APPFOLIO_PHOTO_TARGET_BYTES
    assert len(raw) < len(big)


def test_encode_files_passes_small_images_through() -> None:
    """Photos already under the target should not be recompressed."""
    from backend.app.integrations.appfolio_vendor.service import FileUpload, _encode_files

    small = _noisy_jpeg(200, 150, quality=85)
    encoded = _encode_files([FileUpload(name="thumb.jpg", data=small)])
    raw = base64.b64decode(encoded[0]["file_in_base64"])
    assert raw == small  # bytes preserved exactly


def test_encode_files_passes_non_image_through() -> None:
    """Non-image attachments (PDFs, etc.) must not be touched."""
    from backend.app.integrations.appfolio_vendor.service import FileUpload, _encode_files

    pdf_bytes = b"%PDF-1.4\n" + b"\x00" * 4_000_000
    encoded = _encode_files([FileUpload(name="invoice.pdf", data=pdf_bytes)])
    raw = base64.b64decode(encoded[0]["file_in_base64"])
    assert raw == pdf_bytes


def test_encode_files_falls_back_when_compression_fails() -> None:
    """If Pillow can't open the image, upload the original bytes."""
    from backend.app.integrations.appfolio_vendor.service import FileUpload, _encode_files

    # Bytes that won't decode as any image despite the .jpg extension.
    junk = b"not actually a JPEG" + b"\xff" * 2_000_000
    encoded = _encode_files([FileUpload(name="broken.jpg", data=junk)])
    raw = base64.b64decode(encoded[0]["file_in_base64"])
    assert raw == junk


def test_format_http_exception_falls_back_to_class_name() -> None:
    """An httpx exception with no message must surface SOME description.

    Bare ``WriteTimeout()``, ``RemoteProtocolError()`` and similar can
    have empty ``str(exc)``, which previously produced
    ``"network failure: "`` in the user-facing error.
    """
    from backend.app.integrations.appfolio_vendor.service import _format_http_exception

    assert _format_http_exception(httpx.WriteTimeout("")) == "WriteTimeout"
    assert _format_http_exception(httpx.RemoteProtocolError("")) == "RemoteProtocolError"
    # Non-empty messages pass through unchanged.
    assert _format_http_exception(httpx.ConnectError("dns lookup failed")) == "dns lookup failed"


def test_fmt_work_order_line_prefers_number_for_display() -> None:
    """The Vendor Portal UI shows ``numberForDisplay``, which can differ
    from the API ``id``. The agent's "WO #X" rendering must match what
    the user sees in their portal so they can find it.
    """
    from backend.app.integrations.appfolio_vendor.work_orders import _fmt_work_order_line

    line = _fmt_work_order_line({"id": 999001, "numberForDisplay": "WO-2026-0042"})
    assert "ID: 999001" in line
    assert "#WO-2026-0042" in line
    assert "#999001" not in line


def test_fmt_work_order_line_falls_back_to_id_when_no_display_number() -> None:
    from backend.app.integrations.appfolio_vendor.work_orders import _fmt_work_order_line

    line = _fmt_work_order_line({"id": 999001})
    assert "ID: 999001" in line
    assert "#999001" in line


def test_fmt_work_order_line_underscored_alias_also_supported() -> None:
    """Some endpoints may use snake_case ``number_for_display``."""
    from backend.app.integrations.appfolio_vendor.work_orders import _fmt_work_order_line

    line = _fmt_work_order_line({"id": 1, "number_for_display": "WO-9"})
    assert "#WO-9" in line


def test_fmt_work_order_line_surfaces_customer_id() -> None:
    """Agent needs the customer_id available in listings to route subsequent
    write calls (notes, invoices) to the right property manager.
    """
    from backend.app.integrations.appfolio_vendor.work_orders import _fmt_work_order_line

    line = _fmt_work_order_line({"id": 999001, "customer_id": "cust-9001"})
    assert "customer_id=cust-9001" in line


@pytest.mark.asyncio()
async def test_add_work_order_note_includes_customer_id_in_body() -> None:
    """AppFolio's note POST requires ``customer_id`` at the top level.

    The OAuth migration in #1269 stopped populating ``customer_ids`` on
    the credential, which caused 422-with-empty-body rejections on every
    note attempt. Verified against the SPA via Playwright capture.
    """
    cred = AppFolioCredential(
        user_id="u1",
        jwt="jwt-1",
        fingerprint="fp-1",
        customer_ids=["cust-9001"],
        extra={},
    )
    service = AppFolioVendorService(cred, api_base="https://api.test")
    response = _mock_response(json_data={"id": 42})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.add_work_order_note("999001", body_text="status update")
    _, kwargs = client.request.call_args
    sent = kwargs["json"]
    assert sent["customer_id"] == "cust-9001"
    assert sent["note"] == {"body": "status update"}
    assert sent["files"] == []


@pytest.mark.asyncio()
async def test_add_work_order_note_resolves_customer_id_when_missing() -> None:
    """A credential persisted before the customer_id backfill landed has
    ``customer_ids=[]``. The service must lazy-fetch ``/profiles/me`` so
    existing connected users don't have to disconnect/reconnect.
    """
    cred = AppFolioCredential(
        user_id="u1",
        jwt="jwt-1",
        fingerprint="fp-1",
        customer_ids=[],
        extra={},
    )
    service = AppFolioVendorService(cred, api_base="https://api.test")

    profile_response = _mock_response(
        json_data={"customers": [{"customer_id": "cust-9001", "customer_name": "Acme Properties"}]}
    )
    note_response = _mock_response(json_data={"id": 42})

    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(side_effect=[profile_response, note_response])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.add_work_order_note("999001", body_text="status update")

    # First call: GET /profiles/me to discover customer_id
    first_args, _ = client.request.call_args_list[0]
    assert first_args[0] == "GET"
    assert "/profiles/me" in first_args[1]
    # Second call: POST /notes with the resolved customer_id
    second_args, second_kwargs = client.request.call_args_list[1]
    assert second_args[0] == "POST"
    assert "/notes" in second_args[1]
    assert second_kwargs["json"]["customer_id"] == "cust-9001"
    # The credential should be backfilled in-memory so subsequent calls
    # don't refetch.
    assert cred.customer_ids == ["cust-9001"]


@pytest.mark.asyncio()
async def test_resolve_customer_id_raises_when_profile_has_none() -> None:
    """A credential with no customers and a profile that returns no
    customers should raise rather than silently succeed with bad data.
    """
    cred = AppFolioCredential(
        user_id="u1",
        jwt="jwt-1",
        fingerprint="fp-1",
        customer_ids=[],
        extra={},
    )
    service = AppFolioVendorService(cred, api_base="https://api.test")
    response = _mock_response(json_data={"customers": []})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        with pytest.raises(AppFolioError, match="no customer IDs"):
            await service.add_work_order_note("999001", body_text="x")


def test_client_version_header_is_opaque_hex() -> None:
    """The ``X-Vendor-Portal-Web-Client`` header value must not identify
    Clawbolt to AppFolio. Match the SPA's git-SHA shape (40-char hex)."""
    import re

    from backend.app.integrations.appfolio_vendor.service import _CLIENT_VERSION

    assert re.fullmatch(r"[0-9a-f]{40}", _CLIENT_VERSION) is not None
    assert "clawbolt" not in _CLIENT_VERSION.lower()


@pytest.mark.asyncio()
async def test_service_error_message_omits_response_body() -> None:
    """Raised AppFolioError must not echo the response body.

    The error message flows into ToolResult.content (visible to the LLM
    and the user). The body could include AppFolio-side context we don't
    want to surface; logs are the right place for it.
    """
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    secret = "INTERNAL_AF_TRACE_or_pii_we_dont_want_in_chat"
    response = _mock_response(status_code=500, text=secret)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        with pytest.raises(AppFolioError) as exc_info:
            await service.get("/x")
    msg = str(exc_info.value)
    assert "HTTP 500" in msg
    assert secret not in msg


@pytest.mark.asyncio()
async def test_list_work_orders_passes_filter_params() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"work_orders": []})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.list_work_orders(
            include_in_progress=True,
            include_completed=True,
            include_estimates=False,
            customer_id="cust42",
        )
    args, kwargs = client.request.call_args
    assert args[0] == "GET"
    assert "/maintenance/api/work_orders.json" in args[1]
    assert kwargs["params"] == {
        "includeInProgress": "true",
        "includeCompleted": "true",
        "includeEstimates": "false",
        "customer_id": "cust42",
    }


# ---------------------------------------------------------------------------
# /access exchange + 2FA
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_exchange_magic_link_posts_oauth_token_and_returns_jwt() -> None:
    response = _mock_response(
        json_data={
            "access_token": "jwt-from-server",
            "refresh_token": "rt-1",
            "token_type": "Bearer",
            "expires_in": 7200,
            "scope": "write",
            "created_at": 1778198992,
        }
    )
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        result = await exchange_magic_link(magic_link_token="link-tok")
    assert result.jwt == "jwt-from-server"
    assert result.refresh_token == "rt-1"
    assert result.customer_ids == []
    args, kwargs = client.post.call_args
    assert args[0] == "https://oauth.appf.io/oauth/token"
    assert kwargs["json"]["property_token_credential"] == "link-tok"
    assert kwargs["json"]["client_id"] == "passport-frontend"
    assert kwargs["json"]["grant_type"] == "password"
    assert kwargs["json"]["idp_type"] == "vendor"


@pytest.mark.asyncio()
async def test_exchange_magic_link_raises_when_no_access_token() -> None:
    response = _mock_response(json_data={"some_other_field": "x"})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("post", response)
        with pytest.raises(AppFolioError, match="no access_token"):
            await exchange_magic_link(magic_link_token="t")


@pytest.mark.asyncio()
async def test_exchange_magic_link_propagates_4xx() -> None:
    response = _mock_response(json_data={}, status_code=403)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("post", response)
        with pytest.raises(AppFolioError, match="OAuth exchange failed"):
            await exchange_magic_link(magic_link_token="t")


@pytest.mark.asyncio()
async def test_exchange_magic_link_error_message_omits_response_body() -> None:
    """The OAuth exchange's raised error must not include the response body.

    Same reasoning as ``test_service_error_message_omits_response_body``:
    the message reaches the user via ToolResult.content.
    """
    secret = "appfolio_internal_trace_id=abc123"
    response = _mock_response(json_data={}, status_code=400, text=secret)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("post", response)
        with pytest.raises(AppFolioError) as exc_info:
            await exchange_magic_link(magic_link_token="t")
    msg = str(exc_info.value)
    assert "HTTP 400" in msg
    assert secret not in msg


@pytest.mark.asyncio()
async def test_refresh_access_token_returns_new_jwt() -> None:
    from backend.app.integrations.appfolio_vendor.service import refresh_access_token

    response = _mock_response(
        json_data={
            "access_token": "fresh-jwt",
            "refresh_token": "rt-2",
            "token_type": "Bearer",
            "expires_in": 7200,
        }
    )
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        result = await refresh_access_token(refresh_token="rt-1")
    assert result.jwt == "fresh-jwt"
    assert result.refresh_token == "rt-2"
    args, kwargs = client.post.call_args
    assert args[0] == "https://oauth.appf.io/oauth/token"
    assert kwargs["json"]["grant_type"] == "refresh_token"
    assert kwargs["json"]["refresh_token"] == "rt-1"


@pytest.mark.asyncio()
async def test_service_request_refreshes_on_401_and_retries() -> None:
    from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

    cred = AppFolioCredential(
        user_id="u",
        jwt="old-jwt",
        fingerprint="fp",
        customer_ids=[],
        extra={},
        refresh_token="rt-1",
    )
    refreshed_resp = _mock_response(
        json_data={"access_token": "new-jwt", "refresh_token": "rt-2", "expires_in": 7200}
    )
    api_resp_401 = _mock_response(json_data={"login_url": ""}, status_code=401)
    api_resp_ok = _mock_response(json_data={"ok": True})

    persisted: list[tuple[str, str]] = []

    async def on_refresh(jwt: str, refresh: str) -> None:
        persisted.append((jwt, refresh))

    svc = AppFolioVendorService(cred, api_base="https://api.test", on_token_refresh=on_refresh)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        # Three sequential client uses: API 401, OAuth refresh 200, API retry 200.
        clients = [AsyncMock(), AsyncMock(), AsyncMock()]
        clients[0].request = AsyncMock(return_value=api_resp_401)
        clients[1].post = AsyncMock(return_value=refreshed_resp)
        clients[2].request = AsyncMock(return_value=api_resp_ok)
        cms = []
        for c in clients:
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=c)
            cm.__aexit__ = AsyncMock(return_value=False)
            cms.append(cm)
        cls.side_effect = cms
        out = await svc.get("/profiles/me")
    assert out == {"ok": True}
    assert cred.jwt == "new-jwt"
    assert cred.refresh_token == "rt-2"
    assert persisted == [("new-jwt", "rt-2")]


def test_build_service_passes_on_token_refresh_callback() -> None:
    """build_service must thread the persistence callback into the service."""
    cred = AppFolioCredential(
        user_id="u",
        jwt="j",
        fingerprint="fp",
        customer_ids=[],
        extra={},
    )

    async def cb(_jwt: str, _refresh: str) -> None:
        pass

    svc = build_service(cred, api_base="https://api.test", on_token_refresh=cb)
    assert svc._on_token_refresh is cb


# ---------------------------------------------------------------------------
# Param model smoke tests (Pydantic validation)
# ---------------------------------------------------------------------------


def test_connect_params_requires_magic_link() -> None:
    from pydantic import ValidationError

    AppFolioConnectParams(magic_link="x")
    with pytest.raises(ValidationError):
        AppFolioConnectParams()  # type: ignore[call-arg]


def test_list_work_orders_params_defaults() -> None:
    p = AppFolioListWorkOrdersParams()
    assert p.include_in_progress is True
    assert p.include_completed is False
    assert p.include_estimates is True
    assert p.customer_id == ""


# ---------------------------------------------------------------------------
# Write surface (PR2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_add_note_inlines_base64_files() -> None:
    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"id": "999"})
    files = [FileUpload(name="kitchen.jpg", data=b"\x89PNGfake")]
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.add_work_order_note("42", body_text="arrived", files=files)
    _, kwargs = client.request.call_args
    payload = kwargs["json"]
    assert payload["note"] == {"body": "arrived"}
    assert len(payload["files"]) == 1
    entry = payload["files"][0]
    assert entry["name"] == "kitchen.jpg"
    # base64 of b"\x89PNGfake"
    import base64

    assert entry["file_in_base64"] == base64.b64encode(b"\x89PNGfake").decode("ascii")


@pytest.mark.asyncio()
async def test_add_note_sends_empty_files_array_when_no_attachments() -> None:
    """The SPA always sends ``files: []`` rather than omitting it; we
    mirror that shape so AppFolio's request validator can't get clever
    about a missing-versus-empty distinction."""
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"id": "999"})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.add_work_order_note("42", body_text="status")
    _, kwargs = client.request.call_args
    sent = kwargs["json"]
    assert sent["note"] == {"body": "status"}
    assert sent["files"] == []
    assert sent["customer_id"] == "c1"


# ---------------------------------------------------------------------------
# Media resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_resolve_staged_files_pulls_from_downloaded_media() -> None:
    from backend.app.integrations.appfolio_vendor.media_resolver import (
        resolve_staged_files,
    )
    from backend.app.media.download import DownloadedMedia

    media = DownloadedMedia(
        content=b"\xff\xd8fakejpg",
        mime_type="image/jpeg",
        original_url="https://example.com/photo1.jpg",
        filename="photo1.jpg",
    )
    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = [media]
    ctx.storage = None  # forces the resolver to skip the saved-file fallback

    with (
        patch(
            "backend.app.agent.media_staging.resolve_media_ref",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "backend.app.agent.media_staging.get_all_for_user",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        result = await resolve_staged_files(ctx, ["https://example.com/photo1.jpg"])

    from backend.app.integrations.appfolio_vendor.service import FileUpload

    assert isinstance(result, list)
    assert len(result) == 1
    first = result[0]
    assert isinstance(first, FileUpload)
    assert first.data == b"\xff\xd8fakejpg"
    assert first.name.endswith(".jpg")


@pytest.mark.asyncio()
async def test_resolve_staged_files_returns_error_for_missing_ref() -> None:
    from backend.app.integrations.appfolio_vendor.media_resolver import (
        resolve_staged_files,
    )

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []
    ctx.storage = None

    with (
        patch(
            "backend.app.agent.media_staging.resolve_media_ref",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "backend.app.agent.media_staging.get_all_for_user",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        result = await resolve_staged_files(ctx, ["https://example.com/missing.jpg"])

    # Returns ToolResult on error rather than raising.
    from backend.app.agent.tools.base import ToolResult as _TR

    assert isinstance(result, _TR)
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_resolve_staged_files_empty_list_returns_empty_list() -> None:
    from backend.app.integrations.appfolio_vendor.media_resolver import (
        resolve_staged_files,
    )

    ctx = MagicMock()
    ctx.user.id = "u1"
    result = await resolve_staged_files(ctx, [])
    assert result == []


# ---------------------------------------------------------------------------
# Invoices and estimates
# ---------------------------------------------------------------------------


def _patch_request(response: httpx.Response) -> Any:
    client = AsyncMock()
    client.request = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client


@pytest.mark.asyncio()
async def test_create_invoice_serializes_line_items() -> None:
    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"id": "inv-1"})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cm, client = _patch_request(response)
        cls.return_value = cm
        await service.create_invoice(
            customer_id="cust-1",
            work_order_id="42",
            line_items=[
                {"description": "Labor 4hr", "quantity": 4.0, "amount": 75.0},
                {"description": "Materials", "quantity": 1.0, "amount": 120.0},
            ],
            address={
                "property_or_unit_name": "Acme Bldg Unit 1",
                "address_1": "123 Example Street",
            },
            reference_number="REF-001",
            files=[FileUpload(name="receipt.pdf", data=b"%PDF-fake")],
        )
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    payload = kwargs["json"]
    # SPA-verified shape: snake_case keys, work_order_id as int, line_items
    # with ``amount``, ``address`` block, ``reference_number``.
    assert payload["customer_id"] == "cust-1"
    assert payload["work_order_id"] == 42
    assert payload["line_items"][0]["description"] == "Labor 4hr"
    assert payload["line_items"][0]["amount"] == 75.0
    assert payload["address"]["address_1"] == "123 Example Street"
    assert payload["reference_number"] == "REF-001"
    assert len(payload["files"]) == 1


@pytest.mark.asyncio()
async def test_upload_invoice_pdf_omits_line_items() -> None:
    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cm, client = _patch_request(response)
        cls.return_value = cm
        await service.upload_invoice_pdf(
            customer_id="cust-1",
            work_order_id="999001",
            files=[FileUpload(name="invoice.pdf", data=b"%PDF-fake")],
        )
    _, kwargs = client.request.call_args
    payload = kwargs["json"]
    assert "line_items" not in payload
    assert payload["customer_id"] == "cust-1"
    assert payload["work_order_id"] == 999001
    assert len(payload["files"]) == 1


# ---------------------------------------------------------------------------
# Tool-level validation paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_create_invoice_tool_rejects_empty_line_items() -> None:
    """The tool should validate before any HTTP call."""
    from backend.app.agent.tools.base import ToolErrorKind
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    result = await create.function(customer_id="cust-1", work_order_id="wo-1", line_items=[])
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


@pytest.mark.asyncio()
async def test_create_invoice_tool_rejects_malformed_line_item() -> None:
    """Per-item Pydantic errors surface as a validation ToolResult, not an exception."""
    from backend.app.agent.tools.base import ToolErrorKind
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    # Missing rate; quantity wrong type.
    result = await create.function(
        customer_id="cust-1",
        work_order_id="wo-1",
        line_items=[{"description": "labor", "quantity": "two"}],
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


# ---------------------------------------------------------------------------
# Address auto-fetch — invoice POSTs include the SPA-shaped address block
# ---------------------------------------------------------------------------


def test_address_from_work_order_prefers_structured_fields() -> None:
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {
        "id": 1,
        "property_or_unit_name": "Test Building Unit 1",
        "address_1": "123 Example Street",
        "address_2": "Suite 200",
        "city": "Anytown",
        "state": "CA",
        "zip_code": "99999",
        # Should be ignored when structured fields are present.
        "property_address": "ignored formatted string",
    }
    assert _address_from_work_order(wo) == {
        "property_or_unit_name": "Test Building Unit 1",
        "address_1": "123 Example Street",
        "address_2": "Suite 200",
        "city": "Anytown",
        "state": "CA",
        "zip_code": "99999",
    }


def test_address_from_work_order_accepts_camelcase_aliases() -> None:
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {"address1": "123 Example Street", "postalCode": "99999"}
    assert _address_from_work_order(wo) == {
        "address_1": "123 Example Street",
        "zip_code": "99999",
    }


def test_address_from_work_order_falls_back_to_formatted_string() -> None:
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {"property_address": "123 Example Street, Anytown, CA 99999"}
    assert _address_from_work_order(wo) == {
        "address_1": "123 Example Street, Anytown, CA 99999",
    }


def test_address_from_work_order_returns_empty_when_no_address() -> None:
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    assert _address_from_work_order({"id": 1, "status": "open"}) == {}


def test_address_from_work_order_handles_nested_camelcase_dict() -> None:
    """Production WO responses nest the address as a camelCase dict.

    Reproduces the shape observed in ``list_work_orders`` output:
    ``wo["address"]`` is a dict with ``propertyOrUnitName``,
    ``address1``, ``address2``, ``city``, ``state``, ``zipCode``.
    The parser must descend into that container and translate the
    camelCase keys to the SPA's snake_case shape.
    """
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {
        "id": 113468,
        "address": {
            "propertyOrUnitName": "Test Building Unit 2L",
            "address1": "123 Example Street - 2L",
            "address2": "",
            "city": "Anytown",
            "state": "CA",
            "zipCode": "99999",
        },
    }
    assert _address_from_work_order(wo) == {
        "property_or_unit_name": "Test Building Unit 2L",
        "address_1": "123 Example Street - 2L",
        "city": "Anytown",
        "state": "CA",
        "zip_code": "99999",
    }


def test_address_from_work_order_unwraps_snake_case_envelope() -> None:
    """A WO wrapped in ``{"work_order": {...}}`` (snake_case) extracts cleanly."""
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {
        "work_order": {
            "id": 1,
            "address": {"address1": "123 Example Street", "city": "Anytown"},
        }
    }
    assert _address_from_work_order(wo) == {
        "address_1": "123 Example Street",
        "city": "Anytown",
    }


def test_address_from_work_order_unwraps_camelcase_envelope() -> None:
    """Regression: production WO GET returns ``{"workOrder": {...}}``.

    The diagnostic warning from the previous deploy logged
    ``top_keys=['workOrder']`` for jesse's failing invoice. Pin a
    regression test to that exact envelope key so we don't lose it.
    """
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {
        "workOrder": {
            "id": 1,
            "address": {
                "propertyOrUnitName": "Test Building Unit 2L",
                "address1": "123 Example Street",
                "city": "Anytown",
                "state": "CA",
                "zipCode": "99999",
            },
        }
    }
    assert _address_from_work_order(wo) == {
        "property_or_unit_name": "Test Building Unit 2L",
        "address_1": "123 Example Street",
        "city": "Anytown",
        "state": "CA",
        "zip_code": "99999",
    }


def test_address_from_work_order_top_level_overrides_nested() -> None:
    """Top-level structured fields win over nested ones when both present."""
    from backend.app.integrations.appfolio_vendor.invoices import (
        _address_from_work_order,
    )

    wo = {
        "address_1": "Top Level Street",
        "address": {"address1": "Nested Street"},
    }
    assert _address_from_work_order(wo)["address_1"] == "Top Level Street"


@pytest.mark.asyncio()
async def test_create_invoice_tool_includes_address_from_work_order() -> None:
    """The tool fetches the WO and ships the SPA-shaped address block."""
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    get_wo = AsyncMock(
        return_value={
            "id": 42,
            "address_1": "123 Example Street",
            "city": "Anytown",
            "state": "CA",
            "zip_code": "99999",
        }
    )
    create_invoice = AsyncMock(return_value={"id": "inv-1"})
    service.get_work_order = get_wo  # type: ignore[method-assign]
    service.create_invoice = create_invoice  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    result = await create.function(
        customer_id="cust-1",
        work_order_id="42",
        line_items=[{"description": "Labor 4hr", "quantity": 4.0, "amount": 75.0}],
    )
    assert result.is_error is False
    get_wo.assert_awaited_once_with("cust-1", "42")
    create_invoice.assert_awaited_once()
    assert create_invoice.call_args is not None
    assert create_invoice.call_args.kwargs["address"] == {
        "address_1": "123 Example Street",
        "city": "Anytown",
        "state": "CA",
        "zip_code": "99999",
    }


@pytest.mark.asyncio()
async def test_create_invoice_tool_short_circuits_when_address_extraction_empty() -> None:
    """Empty address extraction surfaces a clear error and skips the POST.

    AppFolio rejects invoice POSTs without an ``address`` block (HTTP
    500 with empty body), so shipping a no-address payload would just
    fail noisily anyway. Surface a clear ToolResult instead and emit a
    warning log so we can debug the response-shape mismatch.
    """
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    create_invoice = AsyncMock(return_value={"id": "inv-1"})
    service.get_work_order = AsyncMock(return_value={"id": 42})  # type: ignore[method-assign]
    service.create_invoice = create_invoice  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    result = await create.function(
        customer_id="cust-1",
        work_order_id="42",
        line_items=[{"description": "Labor", "quantity": 1.0, "amount": 100.0}],
    )
    assert result.is_error is True
    create_invoice.assert_not_awaited()


@pytest.mark.asyncio()
async def test_create_invoice_tool_surfaces_work_order_lookup_failure() -> None:
    """A failed WO lookup short-circuits before the invoice POST."""
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    create_invoice = AsyncMock()
    service.get_work_order = AsyncMock(  # type: ignore[method-assign]
        side_effect=AppFolioError("not found")
    )
    service.create_invoice = create_invoice  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    result = await create.function(
        customer_id="cust-1",
        work_order_id="42",
        line_items=[{"description": "Labor", "quantity": 1.0, "amount": 100.0}],
    )
    assert result.is_error is True
    create_invoice.assert_not_awaited()


@pytest.mark.asyncio()
async def test_upload_invoice_pdf_tool_includes_address_from_work_order() -> None:
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools
    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    upload_pdf = AsyncMock(return_value={"id": "inv-2"})
    service.get_work_order = AsyncMock(  # type: ignore[method-assign]
        return_value={"address_1": "123 Example Street"}
    )
    service.upload_invoice_pdf = upload_pdf  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    staged = FileUpload(name="invoice.pdf", data=b"%PDF-fake")
    with patch(
        "backend.app.integrations.appfolio_vendor.invoices.resolve_staged_files",
        new=AsyncMock(return_value=[staged]),
    ):
        tools = build_invoice_tools(service, ctx)
        upload = next(t for t in tools if t.name == "appfolio_upload_invoice_pdf")
        result = await upload.function(
            customer_id="cust-1",
            work_order_id="42",
            media_refs=["media_abc"],
        )
    assert result.is_error is False
    upload_pdf.assert_awaited_once()
    assert upload_pdf.call_args is not None
    assert upload_pdf.call_args.kwargs["address"] == {"address_1": "123 Example Street"}


@pytest.mark.asyncio()
async def test_create_invoice_tool_falls_back_when_customer_id_scope_rejected() -> None:
    """When the agent's customer_id is wrong, the tool retries with the canonical one.

    Reproduces the production failure mode: the agent extracted a
    ``customer_id`` from a search response (which carries a different
    field than the write endpoints expect), passed it in, and the
    work-order GET answered HTTP 401 with no ``login_url``. The fix
    catches :class:`AuthScopeError`, resolves the canonical customer
    via ``/profiles/me``, retries the GET, and uses the canonical
    value for the subsequent ``create_invoice`` POST.
    """
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    cred = AppFolioCredential(
        user_id="u1",
        jwt="jwt-1",
        fingerprint="fp-1",
        # No cached customer_ids: forces the resolver down the
        # ``/profiles/me`` path so we exercise it end-to-end.
        customer_ids=[],
        extra={"fingerprint": "fp-1"},
    )
    service = AppFolioVendorService(cred, api_base="https://api.test")

    # First get_work_order (with the agent's wrong id) raises scope;
    # the second (with the canonical id) returns the WO dict.
    get_wo = AsyncMock(
        side_effect=[
            AuthScopeError("wrong customer in path"),
            {"address_1": "123 Example Street", "city": "Anytown"},
        ]
    )
    create_invoice = AsyncMock(return_value={"id": "inv-1"})
    get_profile = AsyncMock(return_value={"customers": [{"customer_id": "canonical-cust"}]})
    service.get_work_order = get_wo  # type: ignore[method-assign]
    service.create_invoice = create_invoice  # type: ignore[method-assign]
    service.get_profile = get_profile  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    result = await create.function(
        customer_id="agent-guess",  # wrong, gets corrected internally
        work_order_id="42",
        line_items=[{"description": "Labor", "quantity": 1.0, "amount": 100.0}],
    )

    assert result.is_error is False
    # Two get_work_order calls: first with the agent's guess, second
    # with the resolved canonical id.
    assert get_wo.await_count == 2
    assert get_wo.await_args_list[0].args == ("agent-guess", "42")
    assert get_wo.await_args_list[1].args == ("canonical-cust", "42")
    # The invoice POST uses the canonical id, NOT the agent's guess.
    create_invoice.assert_awaited_once()
    assert create_invoice.call_args is not None
    assert create_invoice.call_args.kwargs["customer_id"] == "canonical-cust"


# ---------------------------------------------------------------------------
# Wire-shape: amount * quantity collapsed before POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_create_invoice_tool_sends_line_total_not_unit_price() -> None:
    """The tool wrapper multiplies quantity by amount before POST.

    Regression for a real billing miscount: a user submitted an invoice
    with ``{quantity: 5, amount: 55}`` expecting a $275 line total. Our
    tool reported $275 to the user but sent ``amount=55`` on the wire,
    and AppFolio stored that as the line total ($55). The tool now
    pre-multiplies on our side so AppFolio's stored line total matches
    what the user (and the tool's own success message) believes.
    """
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    service.get_work_order = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": 42, "address_1": "123 Example Street"}
    )
    create_invoice = AsyncMock(return_value={"id": "inv-1"})
    service.create_invoice = create_invoice  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    result = await create.function(
        customer_id="cust-1",
        work_order_id="42",
        line_items=[
            {"description": "Materials reimbursement", "quantity": 1.0, "amount": 39.07},
            {"description": "Labor", "quantity": 5.0, "amount": 55.0},
        ],
    )
    assert result.is_error is False
    create_invoice.assert_awaited_once()
    assert create_invoice.call_args is not None
    wire_items = create_invoice.call_args.kwargs["line_items"]
    assert wire_items[0]["amount"] == pytest.approx(39.07)
    assert wire_items[0]["quantity"] == "1.0"
    # The fix: line 2 wire amount is qty * unit_price, not just unit_price.
    assert wire_items[1]["amount"] == pytest.approx(275.0)
    assert wire_items[1]["quantity"] == "5.0"
    # User-facing total still matches what AppFolio will store.
    assert "$314.07" in result.content


@pytest.mark.asyncio()
async def test_create_invoice_tool_fractional_quantity_collapses_correctly() -> None:
    """Decimal quantities multiply too (e.g. 1.5 hours at $80/hr = $120)."""
    from backend.app.integrations.appfolio_vendor.invoices import build_invoice_tools

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    service.get_work_order = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": 42, "address_1": "123 Example Street"}
    )
    create_invoice = AsyncMock(return_value={"id": "inv-1"})
    service.create_invoice = create_invoice  # type: ignore[method-assign]

    ctx = MagicMock()
    ctx.user.id = "u1"
    ctx.downloaded_media = []

    tools = build_invoice_tools(service, ctx)
    create = next(t for t in tools if t.name == "appfolio_create_invoice")
    await create.function(
        customer_id="cust-1",
        work_order_id="42",
        line_items=[{"description": "Labor", "quantity": 1.5, "amount": 80.0}],
    )
    create_invoice.assert_awaited_once()
    assert create_invoice.call_args is not None
    wire_items = create_invoice.call_args.kwargs["line_items"]
    assert wire_items[0]["amount"] == pytest.approx(120.0)
    assert wire_items[0]["quantity"] == "1.5"


# ---------------------------------------------------------------------------
# Approval prompt: line-item breakdown so users catch billing mistakes
# ---------------------------------------------------------------------------


def test_create_invoice_approval_description_lists_each_line_with_total() -> None:
    """The approval prompt must show qty x unit = line total per item.

    Before this change the prompt read "Create invoice on AppFolio work
    order #X (N line item(s))", which let an invoice with the wrong
    per-line math slip through unchallenged. The new prompt names every
    line and prints the grand total so users can sanity-check before
    typing yes.
    """
    from backend.app.integrations.appfolio_vendor.invoices import (
        _format_invoice_approval_description,
    )

    description = _format_invoice_approval_description(
        {
            "work_order_id": "114433",
            "line_items": [
                {"description": "Materials reimbursement", "quantity": 1, "amount": 39.07},
                {
                    "description": "Adjusted door for tighter closing",
                    "quantity": 5,
                    "amount": 55.0,
                },
            ],
        }
    )
    # Header carries WO and grand total.
    assert "#114433" in description
    assert "$314.07" in description
    # Each line is enumerated with qty / unit / line total.
    assert "Materials reimbursement" in description
    assert "qty 1 x $39.07 = $39.07" in description
    assert "Adjusted door for tighter closing" in description
    assert "qty 5 x $55.00 = $275.00" in description


def test_create_invoice_approval_description_truncates_long_descriptions() -> None:
    """Very long line descriptions are truncated so the prompt stays scannable."""
    from backend.app.integrations.appfolio_vendor.invoices import (
        _format_invoice_approval_description,
    )

    long = "Adjusted door, weather stripping, new lock, transition strip, " * 5
    description = _format_invoice_approval_description(
        {
            "work_order_id": "1",
            "line_items": [{"description": long, "quantity": 1, "amount": 100.0}],
        }
    )
    # Truncated form ends with an ellipsis and is shorter than the input.
    assert "..." in description
    assert long not in description


def test_create_invoice_approval_description_falls_back_on_malformed_items() -> None:
    """A malformed line item must not raise from the description builder.

    The agent's typed validation will reject the call after approval; the
    prompt just needs to render *something* readable so the approval flow
    does not crash.
    """
    from backend.app.integrations.appfolio_vendor.invoices import (
        _format_invoice_approval_description,
    )

    description = _format_invoice_approval_description(
        {
            "work_order_id": "1",
            "line_items": [{"description": "ok", "quantity": "not-a-number", "amount": 1}],
        }
    )
    # Falls back to the short legacy form rather than raising.
    assert "#1" in description
    assert "1 line item" in description


# ---------------------------------------------------------------------------
# Logging diagnostics — failure paths surface enough info to debug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_4xx_failure_logs_request_body_and_response(caplog: Any) -> None:
    import logging

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(
        json_data={"errors": ["note body required"]},
        status_code=422,
        text='{"errors":["note body required"]}',
    )

    with (
        caplog.at_level(logging.WARNING, logger="backend.app.integrations.appfolio_vendor.service"),
        patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls,
    ):
        cm, _ = _patch_request(response)
        cls.return_value = cm
        with pytest.raises(AppFolioError) as exc_info:
            await service.update_work_order_note("42", "note-7", body_text="updated body text")

    # ToolResult-side message carries the status only; the response body
    # is intentionally redacted so it cannot leak into user-visible chat.
    assert "422" in str(exc_info.value)
    assert "note body required" not in str(exc_info.value)

    # Log line includes everything a dev needs to diagnose.
    record_text = "\n".join(r.message for r in caplog.records)
    assert "PATCH" in record_text
    assert "/maintenance/api/work_orders/42/notes/note-7" in record_text
    assert "updated body text" in record_text  # request body logged
    assert "422" in record_text


@pytest.mark.asyncio()
async def test_4xx_log_summarizes_base64_files(caplog: Any) -> None:
    import logging

    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    big_blob = b"\x00" * 250_000  # large payload
    response = _mock_response(
        json_data={"errors": ["bad"]},
        status_code=400,
        text='{"errors":["bad"]}',
    )

    with (
        caplog.at_level(logging.WARNING, logger="backend.app.integrations.appfolio_vendor.service"),
        patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls,
    ):
        cm, _ = _patch_request(response)
        cls.return_value = cm
        with pytest.raises(AppFolioError):
            await service.add_work_order_note(
                "42", body_text="here", files=[FileUpload(name="big.jpg", data=big_blob)]
            )

    # The base64 content of the photo should NOT appear in logs.
    encoded = __import__("base64").b64encode(big_blob).decode()
    assert encoded not in caplog.text
    # But the file name and a length marker should.
    assert "big.jpg" in caplog.text
    assert "chars base64" in caplog.text  # marker token


@pytest.mark.asyncio()
async def test_appfolio_connect_message_omits_customer_count(async_test_user: Any) -> None:
    """Regression for #1275: connect must not surface 'customer' counts.

    The OAuth2 migration in #1269 stopped populating ``customer_ids`` on
    the exchange result, leaving the receipt forever rendering "0
    customer(s)". Rather than backfill via /profiles/me just to print a
    number that vendors don't think in, we drop the framing entirely.
    """
    from backend.app.agent.tools.names import ToolName
    from backend.app.integrations.appfolio_vendor.auth_tools import build_auth_tools
    from backend.app.integrations.appfolio_vendor.service import AccessExchangeResult

    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    connect = next(t for t in tools if t.name == ToolName.APPFOLIO_CONNECT)

    fake_result = AccessExchangeResult(
        jwt="jwt-1",
        customer_ids=[],
        raw={},
        refresh_token="rt-1",
    )
    with patch(
        "backend.app.integrations.appfolio_vendor.auth_tools.exchange_magic_link",
        new=AsyncMock(return_value=fake_result),
    ):
        result = await connect.function(magic_link="eyJ.fake.token")

    assert result.is_error is False
    assert result.content == "AppFolio connected. Tools are now available."
    assert "customer" not in result.content.lower()
    assert result.receipt is not None
    assert "customer" not in (result.receipt.target or "").lower()


@pytest.mark.asyncio()
async def test_appfolio_connect_parse_failure_hint_steers_to_token_paste(
    async_test_user: Any,
) -> None:
    """Regression for #1297: parse-failure hint must mention the iMessage gotcha.

    When the user pastes a full magic-link URL over iMessage, the SMS
    client strips the query params and the token never reaches us. The
    validation hint has to tell the agent to ask for the token alone, not
    the full URL, so the next attempt actually succeeds.
    """
    from backend.app.agent.tools.names import ToolName
    from backend.app.integrations.appfolio_vendor.auth_tools import build_auth_tools

    tools = build_auth_tools(async_test_user.id)
    connect = next(t for t in tools if t.name == ToolName.APPFOLIO_CONNECT)

    # Empty input triggers MagicLinkError -> the validation hint we care about.
    result = await connect.function(magic_link="")

    assert result.is_error is True
    assert result.hint is not None
    assert "magic_link_token=" in result.hint
    assert "iMessage" in result.hint


@pytest.mark.asyncio()
async def test_access_failure_does_not_log_magic_link(caplog: Any) -> None:
    import logging

    response = _mock_response(
        json_data={"error": "expired"},
        status_code=400,
        text='{"error":"expired"}',
    )

    with (
        caplog.at_level(logging.WARNING, logger="backend.app.integrations.appfolio_vendor.service"),
        patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls,
    ):
        cls.return_value = _patch_async_client("post", response)
        with pytest.raises(AppFolioError):
            from backend.app.integrations.appfolio_vendor.service import (
                exchange_magic_link,
            )

            await exchange_magic_link(magic_link_token="SUPER_SECRET_TOKEN")

    assert "SUPER_SECRET_TOKEN" not in caplog.text
    # Status and response body should still be in the log so we can debug.
    assert "400" in caplog.text
    assert "expired" in caplog.text


# ---------------------------------------------------------------------------
# Factory + registry wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_auth_tools_always_in_schema_when_disconnected() -> None:
    """``appfolio_connect`` must stay reachable when the user has no credential.

    Magic-link integrations need the auth tool on the schema regardless
    of connection state, since pasting the token *is* the connect path.
    The auth tools live in a separate core factory (``appfolio_auth``) so
    the data factory's auth_check can correctly report "not connected"
    without stripping the connect path. Regression for the prod bug where
    the agent confidently told users "AppFolio is connected" before they
    had connected, and for the original dev.clawbolt.ai bug where the
    agent had no way to start the connect flow.
    """
    from backend.app.agent.tools.names import ToolName
    from backend.app.agent.tools.registry import (
        ToolContext,
        default_registry,
        ensure_tool_modules_imported,
    )
    from backend.app.models import User

    ensure_tool_modules_imported()

    user = User(id="appfolio-disconnected-user", user_id="test")
    ctx = ToolContext(user=user)

    # The data factory's auth_check returns a reason when not connected,
    # so the registry will surface ``appfolio_vendor`` under "Not
    # connected" in list_capabilities and the LLM will know it must
    # connect first before claiming AppFolio access.
    data_factory = default_registry._factories["appfolio_vendor"]
    assert data_factory.auth_check is not None
    with patch(
        "backend.app.integrations.appfolio_vendor.factory.load_credential",
        new=AsyncMock(return_value=None),
    ):
        reason = await data_factory.auth_check(ctx)
    assert reason is not None
    assert "not connected" in reason.lower()

    # The auth factory is separate, core, and always materializes the
    # connect tools so the LLM has a way to drive the magic-link flow.
    from backend.app.integrations.appfolio_vendor.factory import (
        _appfolio_auth_factory,
        _appfolio_vendor_factory,
    )

    auth_tools = await _appfolio_auth_factory(ctx)
    auth_names = {t.name for t in auth_tools}
    assert ToolName.APPFOLIO_CONNECT in auth_names

    # The data factory returns nothing when the credential is missing.
    with patch(
        "backend.app.integrations.appfolio_vendor.factory.load_credential",
        new=AsyncMock(return_value=None),
    ):
        data_tools = await _appfolio_vendor_factory(ctx)
    data_names = {t.name for t in data_tools}
    assert ToolName.APPFOLIO_LIST_WORK_ORDERS not in data_names
    assert ToolName.APPFOLIO_CONNECT not in data_names


@pytest.mark.asyncio()
async def test_unconnected_user_sees_appfolio_in_unauthenticated_list() -> None:
    """When the user has no AppFolio credential, ``list_capabilities`` must
    show ``appfolio_vendor`` under "Not connected", not in the available
    specialists list. Regression for the prod bug where the agent told a
    first-time user "Yeah, AppFolio is connected" before the user had
    pasted any magic link, because the registry treated AppFolio as a
    ready specialist regardless of credential state.
    """
    from backend.app.agent.tools.registry import (
        ToolContext,
        default_registry,
        ensure_tool_modules_imported,
    )
    from backend.app.models import User

    ensure_tool_modules_imported()

    user = User(id="appfolio-unconnected", user_id="test")
    ctx = ToolContext(user=user)

    with patch(
        "backend.app.integrations.appfolio_vendor.factory.load_credential",
        new=AsyncMock(return_value=None),
    ):
        ready = await default_registry.get_available_specialist_summaries(ctx)
        unauth = await default_registry.get_unauthenticated_specialists(ctx)

    assert "appfolio_vendor" not in ready, (
        "appfolio_vendor must NOT appear as a ready specialist for an "
        "unconnected user; it should be in the unauthenticated list so "
        "the LLM knows to guide the user through connecting first."
    )
    assert "appfolio_vendor" in unauth
    assert "not connected" in unauth["appfolio_vendor"].lower()


@pytest.mark.asyncio()
async def test_connected_user_sees_appfolio_in_specialist_summaries() -> None:
    """The complementary case: when the user has a usable credential,
    ``appfolio_vendor`` must show up as a ready specialist. The summary
    should mention write capabilities (notes, photos, invoices) so the
    LLM knows it can act on work orders, not just read them.
    """
    from backend.app.agent.tools.registry import (
        ToolContext,
        default_registry,
        ensure_tool_modules_imported,
    )
    from backend.app.models import User

    ensure_tool_modules_imported()

    user = User(id="appfolio-connected", user_id="test")
    ctx = ToolContext(user=user)

    cred = AppFolioCredential(
        user_id=user.id,
        jwt="eyJ.fake.jwt",
        fingerprint="abc123",
        customer_ids=["cust-1"],
        extra={},
    )
    with patch(
        "backend.app.integrations.appfolio_vendor.factory.load_credential",
        new=AsyncMock(return_value=cred),
    ):
        ready = await default_registry.get_available_specialist_summaries(ctx)
        unauth = await default_registry.get_unauthenticated_specialists(ctx)

    assert "appfolio_vendor" in ready
    assert "appfolio_vendor" not in unauth
    summary = ready["appfolio_vendor"].lower()
    # Read-only language alone caused the agent to refuse writes in prod
    # ("AppFolio is read-only on my end..."). The summary must surface
    # write capabilities so the LLM knows the full surface area.
    assert "note" in summary or "invoice" in summary
