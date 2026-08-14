"""Regression tests for the OAuth redirect_uri alignment.

The bug: the authorization-redirect step (router) and the
token-exchange step (oauth_flow) used to read the redirect URI from
two different settings (``APP_BASE_URL``-derived vs the legacy
``GOOGLE_REDIRECT_URI``). When operators set ``APP_BASE_URL`` for a
new dev/prod hostname but forgot to update ``GOOGLE_REDIRECT_URI``,
Google rejected the token exchange with HTTP 400 because the
``redirect_uri`` in the token exchange didn't match the one sent at
auth time.

These tests pin the post-fix behavior: both call sites derive the
URI from ``APP_BASE_URL`` so they're guaranteed to match.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.auth.oauth_flow import (
    _default_redirect_uri,
    exchange_google_code,
)
from backend.app.config import settings
from backend.app.routers.google_oauth import _get_redirect_uri


def test_router_and_oauth_flow_compute_the_same_uri() -> None:
    """Both helpers compute the same callback URL from APP_BASE_URL.

    If these ever drift (e.g. a refactor moves one but not the other),
    Google's token endpoint will start rejecting auth flows in
    production. This test fails loudly before that ships.
    """
    assert _get_redirect_uri() == _default_redirect_uri()


def test_default_redirect_uri_uses_app_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies the URI tracks ``APP_BASE_URL`` rather than a separate
    setting. Operators changing the deployment hostname only need to
    update one env var.
    """
    monkeypatch.setattr(settings, "app_base_url", "https://dev.clawbolt.ai")
    assert _default_redirect_uri() == ("https://dev.clawbolt.ai/api/auth/oauth/google/callback")

    # Trailing slash on the base URL must NOT produce a double-slash
    # path. Google's redirect-URI matcher is exact-string; even one
    # extra slash trips a 400.
    monkeypatch.setattr(settings, "app_base_url", "https://dev.clawbolt.ai/")
    assert _default_redirect_uri() == ("https://dev.clawbolt.ai/api/auth/oauth/google/callback")


@pytest.mark.asyncio
async def test_exchange_google_code_uses_explicit_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the router passes ``redirect_uri=...`` explicitly, that
    value lands in the token-exchange POST body verbatim — even if
    ``APP_BASE_URL`` says something different.

    This is the route the live callback takes: the router computed
    its own URI for the auth redirect and passes the SAME URI back
    here so the two are guaranteed to match.
    """
    captured: dict[str, dict[str, str]] = {}

    class _MockResponse:
        def __init__(self, payload: dict[str, str]) -> None:
            self._payload = payload

        def json(self) -> dict[str, str]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    class _MockClient:
        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, data: dict[str, str]) -> _MockResponse:
            captured["data"] = data
            return _MockResponse({"access_token": "fake-access"})

        async def get(self, url: str, headers: dict[str, str]) -> _MockResponse:
            return _MockResponse({"sub": "fake-sub", "email": "fake@example.com"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _MockClient())

    info = await exchange_google_code(
        "abc-code", redirect_uri="https://explicit.example.com/callback"
    )
    assert info["sub"] == "fake-sub"
    assert captured["data"]["redirect_uri"] == "https://explicit.example.com/callback"


@pytest.mark.asyncio
async def test_exchange_google_code_default_uri_tracks_app_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy callers that don't pass ``redirect_uri`` get the
    ``APP_BASE_URL``-derived URI, NOT the legacy ``GOOGLE_REDIRECT_URI``.
    Pre-fix, this branch read the wrong value and broke prod.
    """
    monkeypatch.setattr(settings, "app_base_url", "https://dev.clawbolt.ai")
    # Set the legacy setting to a deliberately WRONG value to prove the
    # fix doesn't accidentally fall back to it.
    monkeypatch.setattr(
        settings,
        "google_redirect_uri",
        "http://wrong-host.example/api/auth/oauth/google/callback",
    )

    captured: dict[str, dict[str, str]] = {}

    class _MockResponse:
        def json(self) -> dict[str, str]:
            return {"access_token": "fake", "sub": "fake-sub"}

        def raise_for_status(self) -> None:
            return None

    class _MockClient:
        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, data: dict[str, str]) -> _MockResponse:
            captured["data"] = data
            return _MockResponse()

        async def get(self, url: str, headers: dict[str, str]) -> _MockResponse:
            return _MockResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _MockClient())

    await exchange_google_code("abc-code")
    sent = captured["data"]["redirect_uri"]
    assert sent == "https://dev.clawbolt.ai/api/auth/oauth/google/callback"
    assert "wrong-host" not in sent
