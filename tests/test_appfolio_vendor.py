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
async def test_service_401_raises_auth_expired_with_login_url() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={"login_url": "https://login/here"}, status_code=401)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("request", response)
        with pytest.raises(AuthExpiredError) as exc_info:
            await service.get("/anything")
    assert exc_info.value.login_url == "https://login/here"


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
    raw = base64.b64decode(encoded[0]["file_base64"])
    assert len(raw) <= _APPFOLIO_PHOTO_TARGET_BYTES
    assert len(raw) < len(big)


def test_encode_files_passes_small_images_through() -> None:
    """Photos already under the target should not be recompressed."""
    from backend.app.integrations.appfolio_vendor.service import FileUpload, _encode_files

    small = _noisy_jpeg(200, 150, quality=85)
    encoded = _encode_files([FileUpload(name="thumb.jpg", data=small)])
    raw = base64.b64decode(encoded[0]["file_base64"])
    assert raw == small  # bytes preserved exactly


def test_encode_files_passes_non_image_through() -> None:
    """Non-image attachments (PDFs, etc.) must not be touched."""
    from backend.app.integrations.appfolio_vendor.service import FileUpload, _encode_files

    pdf_bytes = b"%PDF-1.4\n" + b"\x00" * 4_000_000
    encoded = _encode_files([FileUpload(name="invoice.pdf", data=pdf_bytes)])
    raw = base64.b64decode(encoded[0]["file_base64"])
    assert raw == pdf_bytes


def test_encode_files_falls_back_when_compression_fails() -> None:
    """If Pillow can't open the image, upload the original bytes."""
    from backend.app.integrations.appfolio_vendor.service import FileUpload, _encode_files

    # Bytes that won't decode as any image despite the .jpg extension.
    junk = b"not actually a JPEG" + b"\xff" * 2_000_000
    encoded = _encode_files([FileUpload(name="broken.jpg", data=junk)])
    raw = base64.b64decode(encoded[0]["file_base64"])
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

    line = _fmt_work_order_line({"id": 114433, "numberForDisplay": "WO-2026-0042"})
    assert "ID: 114433" in line
    assert "#WO-2026-0042" in line
    assert "#114433" not in line


def test_fmt_work_order_line_falls_back_to_id_when_no_display_number() -> None:
    from backend.app.integrations.appfolio_vendor.work_orders import _fmt_work_order_line

    line = _fmt_work_order_line({"id": 114433})
    assert "ID: 114433" in line
    assert "#114433" in line


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

    line = _fmt_work_order_line({"id": 114433, "customer_id": "1963538"})
    assert "customer_id=1963538" in line


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
        customer_ids=["1963538"],
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
        await service.add_work_order_note("114433", body_text="status update")
    _, kwargs = client.request.call_args
    sent = kwargs["json"]
    assert sent["customer_id"] == "1963538"
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
        json_data={"customers": [{"customer_id": 1963538, "customer_name": "Arbors"}]}
    )
    note_response = _mock_response(json_data={"id": 42})

    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(side_effect=[profile_response, note_response])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.add_work_order_note("114433", body_text="status update")

    # First call: GET /profiles/me to discover customer_id
    first_args, _ = client.request.call_args_list[0]
    assert first_args[0] == "GET"
    assert "/profiles/me" in first_args[1]
    # Second call: POST /notes with the resolved customer_id
    second_args, second_kwargs = client.request.call_args_list[1]
    assert second_args[0] == "POST"
    assert "/notes" in second_args[1]
    assert second_kwargs["json"]["customer_id"] == "1963538"
    # The credential should be backfilled in-memory so subsequent calls
    # don't refetch.
    assert cred.customer_ids == ["1963538"]


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
            await service.add_work_order_note("114433", body_text="x")


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


@pytest.mark.asyncio()
async def test_list_payments_emits_filter_brackets() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data=[])
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.list_payments(posted_on="2026-01-01", settlement_method="e_check")
    _, kwargs = client.request.call_args
    assert kwargs["params"] == {
        "filter[posted_on]": "2026-01-01",
        "filter[settlement_method]": "e_check",
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
async def test_accept_work_order_passes_ref_param() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.accept_work_order("42", body={"notes": "got it"})
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    assert "/maintenance/api/work_orders/42/accept" in args[1]
    assert kwargs["params"] == {"ref": "vendor_portal"}
    assert kwargs["json"] == {"notes": "got it"}


@pytest.mark.asyncio()
async def test_schedule_work_order_omits_unset_fields() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.schedule_work_order("42", scheduled_at="2026-05-08T14:00:00-04:00")
    _, kwargs = client.request.call_args
    assert kwargs["json"] == {"scheduledAt": "2026-05-08T14:00:00-04:00"}


@pytest.mark.asyncio()
async def test_schedule_work_order_includes_optional_fields() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.schedule_work_order(
            "42",
            scheduled_at="2026-05-08T14:00:00",
            duration_minutes=90,
            notes="bring ladder",
        )
    _, kwargs = client.request.call_args
    assert kwargs["json"] == {
        "scheduledAt": "2026-05-08T14:00:00",
        "durationMinutes": 90,
        "notes": "bring ladder",
    }


@pytest.mark.asyncio()
async def test_update_status_uses_camelcase_envelope() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        await service.update_work_order_status("42", status_code=8)
    args, kwargs = client.request.call_args
    assert args[0] == "PATCH"
    assert kwargs["json"] == {"workOrder": {"statusCode": 8}}


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

    assert entry["file_base64"] == base64.b64encode(b"\x89PNGfake").decode("ascii")


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


@pytest.mark.asyncio()
async def test_message_tenant_two_step_flow() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    proxy_resp = _mock_response(json_data={"phone_number": "+15551234567"})
    msg_resp = _mock_response(json_data={"ok": True})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        # service.get_proxy_number then service.message_tenant
        client.request = AsyncMock(side_effect=[proxy_resp, msg_resp])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        proxy = await service.get_proxy_number("42")
        await service.message_tenant(
            work_order_id="42",
            phone_number=proxy["phone_number"],
            message="on my way",
        )
    assert client.request.call_count == 2
    second_args, second_kwargs = client.request.call_args_list[1]
    assert second_args[0] == "POST"
    assert second_args[1].endswith("/tenant_vendor_conversations")
    assert second_kwargs["json"] == {
        "work_order_id": "42",
        "phone_number": "+15551234567",
        "message": "on my way",
    }


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
            return_value=None,
        ),
        patch(
            "backend.app.agent.media_staging.get_all_for_user",
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
            return_value=None,
        ),
        patch(
            "backend.app.agent.media_staging.get_all_for_user",
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
# PR3: invoices, compliance, estimates, profile update
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
            work_order_id="wo-1",
            line_items=[
                {"description": "Labor 4hr", "quantity": 4.0, "rate": 75.0},
                {"description": "Materials", "quantity": 1.0, "rate": 120.0},
            ],
            invoice_number="INV-001",
            due_date="2026-06-01",
            files=[FileUpload(name="receipt.pdf", data=b"%PDF-fake")],
        )
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    payload = kwargs["json"]
    assert payload["customerId"] == "cust-1"
    assert payload["workOrderId"] == "wo-1"
    assert payload["lineItems"][0]["description"] == "Labor 4hr"
    assert payload["invoiceNumber"] == "INV-001"
    assert payload["dueDate"] == "2026-06-01"
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
            work_order_id="wo-1",
            files=[FileUpload(name="invoice.pdf", data=b"%PDF-fake")],
        )
    _, kwargs = client.request.call_args
    payload = kwargs["json"]
    assert "lineItems" not in payload
    assert payload["customerId"] == "cust-1"
    assert payload["workOrderId"] == "wo-1"
    assert len(payload["files"]) == 1


@pytest.mark.asyncio()
async def test_upload_compliance_document_uses_singular_file() -> None:
    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cm, client = _patch_request(response)
        cls.return_value = cm
        await service.upload_compliance_document(
            customer_id="cust-1",
            compliance_type="w9",
            file=FileUpload(name="w9.pdf", data=b"%PDF-fake"),
        )
    _, kwargs = client.request.call_args
    payload = kwargs["json"]
    assert payload["customerId"] == "cust-1"
    assert payload["complianceType"] == "w9"
    assert isinstance(payload["file"], dict)
    assert payload["file"]["name"] == "w9.pdf"
    assert "files" not in payload


@pytest.mark.asyncio()
async def test_update_estimate_wraps_jsonapi_envelope() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cm, client = _patch_request(response)
        cls.return_value = cm
        await service.update_estimate("est-7", attributes={"amount": 250.0})
    args, kwargs = client.request.call_args
    assert args[0] == "PATCH"
    assert "/api/estimates/est-7" in args[1]
    assert kwargs["json"] == {
        "data": {"id": "est-7", "type": "estimates", "attributes": {"amount": 250.0}}
    }


@pytest.mark.asyncio()
async def test_update_profile_includes_only_set_fields() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(json_data={})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cm, client = _patch_request(response)
        cls.return_value = cm
        await service.update_profile(phone_number="+15551234567")
    _, kwargs = client.request.call_args
    assert kwargs["json"] == {"user": {"phoneNumber": "+15551234567"}}


@pytest.mark.asyncio()
async def test_update_profile_rejects_empty_payload() -> None:
    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    with pytest.raises(ValueError, match="at least one field"):
        await service.update_profile()


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
# Logging diagnostics — failure paths surface enough info to debug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_4xx_failure_logs_request_body_and_response(caplog: Any) -> None:
    import logging

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    response = _mock_response(
        json_data={"errors": ["scheduledAt: must be in the future"]},
        status_code=422,
        text='{"errors":["scheduledAt: must be in the future"]}',
    )

    with (
        caplog.at_level(logging.WARNING, logger="backend.app.integrations.appfolio_vendor.service"),
        patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls,
    ):
        cm, _ = _patch_request(response)
        cls.return_value = cm
        with pytest.raises(AppFolioError) as exc_info:
            await service.schedule_work_order("42", scheduled_at="2024-01-01T00:00:00")

    # ToolResult-side message carries the status only; the response body
    # is intentionally redacted so it cannot leak into user-visible chat.
    assert "422" in str(exc_info.value)
    assert "scheduledAt" not in str(exc_info.value)

    # Log line includes everything a dev needs to diagnose.
    record_text = "\n".join(r.message for r in caplog.records)
    assert "POST" in record_text
    assert "/maintenance/api/work_orders/42/schedule" in record_text
    assert "scheduledAt" in record_text  # request body logged
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
async def test_4xx_log_summarizes_singular_file_compliance_upload(caplog: Any) -> None:
    """Compliance uploads use singular ``file: {...}``; the marker still applies."""
    import logging

    from backend.app.integrations.appfolio_vendor.service import FileUpload

    service = AppFolioVendorService(_credential(), api_base="https://api.test")
    big_blob = b"\x00" * 250_000
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
            await service.upload_compliance_document(
                customer_id="cust-1",
                compliance_type="w9",
                file=FileUpload(name="w9.pdf", data=big_blob),
            )

    encoded = __import__("base64").b64encode(big_blob).decode()
    assert encoded not in caplog.text
    assert "w9.pdf" in caplog.text
    assert "chars base64" in caplog.text


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
    assert "note" in summary or "invoice" in summary or "schedule" in summary
