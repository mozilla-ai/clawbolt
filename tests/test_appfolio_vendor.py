"""Tests for the AppFolio Vendor Portal integration."""

from __future__ import annotations

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
    submit_two_factor,
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
async def test_exchange_magic_link_returns_jwt_and_customer_ids() -> None:
    response = _mock_response(
        json_data={
            "access_token": "jwt-from-server",
            "customer_ids": ["c1", "c2"],
            "requires_two_factor": False,
        }
    )
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        result = await exchange_magic_link(
            api_base="https://api.test",
            magic_link_token="link-tok",
            fingerprint="fp",
        )
    assert result.jwt == "jwt-from-server"
    assert result.customer_ids == ["c1", "c2"]
    assert result.requires_two_factor is False
    # Token sent both as query param and Bearer header.
    _, kwargs = client.post.call_args
    assert kwargs["params"]["magic_link_token"] == "link-tok"
    assert kwargs["headers"]["Authorization"] == "Bearer link-tok"
    assert kwargs["headers"]["X-Fingerprint"] == "fp"
    assert kwargs["json"]["fingerprint"] == "fp"


@pytest.mark.asyncio()
async def test_exchange_magic_link_extracts_token_from_jwt_field() -> None:
    response = _mock_response(json_data={"jwt": "alt-shape-jwt"})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("post", response)
        result = await exchange_magic_link(
            api_base="https://api.test", magic_link_token="t", fingerprint="fp"
        )
    assert result.jwt == "alt-shape-jwt"


@pytest.mark.asyncio()
async def test_exchange_magic_link_raises_when_no_token_returned() -> None:
    response = _mock_response(json_data={"some_other_field": "x"})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("post", response)
        with pytest.raises(AppFolioError, match="did not return a bearer token"):
            await exchange_magic_link(
                api_base="https://api.test",
                magic_link_token="t",
                fingerprint="fp",
            )


@pytest.mark.asyncio()
async def test_exchange_magic_link_propagates_4xx() -> None:
    response = _mock_response(json_data={}, status_code=403)
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        cls.return_value = _patch_async_client("post", response)
        with pytest.raises(AppFolioError, match="/access exchange failed"):
            await exchange_magic_link(
                api_base="https://api.test",
                magic_link_token="t",
                fingerprint="fp",
            )


@pytest.mark.asyncio()
async def test_submit_two_factor_posts_expected_body() -> None:
    response = _mock_response(json_data={"ok": True})
    with patch("backend.app.integrations.appfolio_vendor.service.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = cm
        out = await submit_two_factor(
            api_base="https://api.test",
            jwt="jwt-1",
            fingerprint="fp",
            code="123456",
        )
    assert out == {"ok": True}
    args, kwargs = client.post.call_args
    assert "/two_factor_authentication/onboard" in args[0]
    assert kwargs["json"] == {"twoFactorToken": {"twoFactorToken": "123456"}}
    assert kwargs["headers"]["Authorization"] == "Bearer jwt-1"


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
async def test_add_note_omits_files_key_when_empty() -> None:
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
    assert kwargs["json"] == {"note": {"body": "status"}}


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

    # ToolResult-side message includes status + response body.
    assert "422" in str(exc_info.value)
    assert "scheduledAt" in str(exc_info.value)

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

            await exchange_magic_link(
                api_base="https://api.test",
                magic_link_token="SUPER_SECRET_TOKEN",
                fingerprint="FP_SECRET",
            )

    assert "SUPER_SECRET_TOKEN" not in caplog.text
    assert "FP_SECRET" not in caplog.text
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
    assert ToolName.APPFOLIO_COMPLETE_2FA in auth_names

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
