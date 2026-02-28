"""Tests for Cloudflare Tunnel discovery and Telegram webhook registration."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from backend.app.services.webhook import discover_tunnel_url, register_telegram_webhook

CLOUDFLARED_QUICKTUNNEL_RESPONSE = {
    "hostname": "random-words.trycloudflare.com",
}


@pytest.mark.asyncio
async def test_discover_tunnel_url_success() -> None:
    """Returns HTTPS URL from cloudflared metrics API response."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = CLOUDFLARED_QUICKTUNNEL_RESPONSE

    with patch("backend.app.services.webhook.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        url = await discover_tunnel_url(max_retries=1, delay=0.0)

    assert url == "https://random-words.trycloudflare.com"


@pytest.mark.asyncio
async def test_discover_tunnel_url_retries_on_failure() -> None:
    """Retries when cloudflared isn't ready, then succeeds."""
    error_response = httpx.Response(status_code=502, request=httpx.Request("GET", "http://x"))

    success_response = Mock()
    success_response.status_code = 200
    success_response.raise_for_status = Mock()
    success_response.json.return_value = CLOUDFLARED_QUICKTUNNEL_RESPONSE

    with patch("backend.app.services.webhook.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = [
            httpx.HTTPStatusError("bad", request=error_response.request, response=error_response),
            success_response,
        ]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        url = await discover_tunnel_url(max_retries=2, delay=0.0)

    assert url == "https://random-words.trycloudflare.com"
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_discover_tunnel_url_returns_none_after_max_retries() -> None:
    """Returns None after exhausting retries."""
    with patch("backend.app.services.webhook.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        url = await discover_tunnel_url(max_retries=3, delay=0.0)

    assert url is None
    assert mock_client.get.call_count == 3


@pytest.mark.asyncio
async def test_register_webhook_success() -> None:
    """Calls Telegram API and returns True on success."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {"ok": True, "result": True}

    with patch("backend.app.services.webhook.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await register_telegram_webhook(
            bot_token="123:ABC",
            webhook_url="https://random-words.trycloudflare.com/api/webhooks/telegram",
            secret="mysecret",
        )

    assert result is True
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.kwargs["json"]["url"] == (
        "https://random-words.trycloudflare.com/api/webhooks/telegram"
    )
    assert call_kwargs.kwargs["json"]["secret_token"] == "mysecret"


@pytest.mark.asyncio
async def test_register_webhook_failure() -> None:
    """Handles API error and returns False."""
    with patch("backend.app.services.webhook.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("network error")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await register_telegram_webhook(
            bot_token="123:ABC",
            webhook_url="https://random-words.trycloudflare.com/api/webhooks/telegram",
        )

    assert result is False
