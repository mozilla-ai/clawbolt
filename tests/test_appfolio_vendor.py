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
) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {"content-type": "application/json"}
    resp.content = b"{}" if json_data is None else b'{"x": 1}'
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = "stub error body"
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
