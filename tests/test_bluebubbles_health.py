"""Tests for BlueBubbles health probing and webhook-registration verification.

Two failure modes drive this module, and neither raises an exception:

- A wrong server password answers HTTP 401. The old reachability check
  accepted any status below 500, so a bridge that could not pass a single
  message reported as healthy.
- Webhook registration runs once at startup. If it fails (Mac asleep), the
  bridge later comes back, every reachability check goes green, and inbound
  iMessage stays dead until somebody redeploys.
"""

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from backend.app.channels.bluebubbles import (
    BlueBubblesChannel,
    BlueBubblesHealth,
    _derive_webhook_token,
    build_webhook_url,
    describe_send_readiness,
    list_bluebubbles_webhooks,
    probe_bluebubbles_server,
    verify_webhook_registration,
)
from backend.app.config import settings

_SERVER = "http://bluebubbles.example"
_PASSWORD = "bb-test-password"


@contextmanager
def _mock_bb_http(handler: Any) -> Any:
    """Route the module's ad-hoc ``httpx.AsyncClient()`` calls to *handler*.

    The probe and webhook helpers create their own short-lived clients rather
    than reusing the channel's, so there is no client attribute to swap.
    """
    real_client = httpx.AsyncClient

    def factory(*_args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("base_url", None)
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with patch("backend.app.channels.bluebubbles.httpx.AsyncClient", factory):
        yield


def _info_response(**data: Any) -> Any:
    """Build a ``/api/v1/server/info`` handler returning *data* in the envelope."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "message": "Success", "data": data})

    return handler


# ---------------------------------------------------------------------------
# Server probe
# ---------------------------------------------------------------------------


async def test_probe_reports_healthy_server_with_flags() -> None:
    """A 200 with readiness flags yields an authenticated, fully-populated result."""
    with _mock_bb_http(
        _info_response(
            server_version="1.9.8",
            private_api=True,
            helper_connected=True,
            detected_icloud="operator@example.com",
        )
    ):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.ok
    assert health.reachable and health.authenticated
    assert health.server_version == "1.9.8"
    assert health.private_api is True
    assert health.helper_connected is True
    assert health.imessage_signed_in is True


async def test_probe_does_not_carry_the_icloud_address() -> None:
    """The account address is reduced to a bool so PII never reaches a log or email."""
    with _mock_bb_http(_info_response(detected_icloud="operator@example.com")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.imessage_signed_in is True
    assert "operator@example.com" not in repr(health)


async def test_probe_treats_401_as_authentication_failure() -> None:
    """Regression: HTTP 401 used to pass ``status_code < 500`` and read as healthy."""
    with _mock_bb_http(lambda _req: httpx.Response(401, text="Unauthorized")):
        health = await probe_bluebubbles_server(_SERVER, "wrong-password")

    assert not health.ok
    assert health.reachable is True
    assert health.authenticated is False
    assert "password" in health.detail.lower()


async def test_probe_treats_404_as_unauthenticated_with_url_hint() -> None:
    """A URL that is not a BlueBubbles server answers 404 rather than failing to connect."""
    with _mock_bb_http(lambda _req: httpx.Response(404, text="Not Found")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert not health.ok
    assert "BLUEBUBBLES_SERVER_URL" in health.detail


async def test_probe_reports_5xx_as_unreachable() -> None:
    with _mock_bb_http(lambda _req: httpx.Response(503, text="down")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.reachable is False
    assert "503" in health.detail


async def test_probe_connect_error_does_not_leak_the_password() -> None:
    """httpx exception text can include the request URL, and the URL carries the password."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)

    with _mock_bb_http(handler):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.reachable is False
    assert _PASSWORD not in health.detail
    assert "ConnectError" in health.detail


async def test_probe_non_json_body_is_not_authenticated() -> None:
    with _mock_bb_http(lambda _req: httpx.Response(200, text="<html>login</html>")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert not health.ok
    assert "non-JSON" in health.detail


async def test_probe_omitted_flags_are_unknown_not_false() -> None:
    """Older servers omit these fields; a missing field must not invent an outage."""
    with _mock_bb_http(_info_response(server_version="1.0.0")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.ok
    assert health.private_api is None
    assert health.helper_connected is None
    assert health.imessage_signed_in is None
    assert describe_send_readiness(health, "private-api") == ""


async def test_probe_survives_a_malformed_server_url() -> None:
    """``httpx.InvalidURL`` is not an ``httpx.HTTPError``; the probe must not raise.

    ``start()`` awaits this before spawning the health and backfill loops, so an
    escape here would silently leave both loops unstarted for the process life.
    """
    health = await probe_bluebubbles_server("http://[::1", _PASSWORD, timeout=0.1)

    assert not health.ok
    assert health.reachable is False
    assert "InvalidURL" in health.detail


# ---------------------------------------------------------------------------
# Send readiness
# ---------------------------------------------------------------------------


async def test_send_readiness_flags_signed_out_mac() -> None:
    """Reachable and authenticated, but Messages.app is signed out: every send fails."""
    with _mock_bb_http(_info_response(detected_icloud="", detected_imessage="")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.ok
    assert health.imessage_signed_in is False
    assert "not signed in to iMessage" in describe_send_readiness(health, "apple-script")


async def test_send_readiness_trusts_the_imessage_account_when_icloud_is_absent() -> None:
    """``detected_icloud`` reads the iCloud login, which a signed-in Mac can lack.

    It comes from MobileMeAccounts.plist, so a Mac using iMessage without iCloud
    (or one where the plist read fails) reports null there while
    ``detected_imessage``, derived from the chat database, is populated. Alerting
    on the iCloud field alone would email the operator about a working bridge.
    """
    with _mock_bb_http(_info_response(detected_icloud=None, detected_imessage="a@example.com")):
        health = await probe_bluebubbles_server(_SERVER, _PASSWORD)

    assert health.imessage_signed_in is True
    assert describe_send_readiness(health, "apple-script") == ""
    assert "a@example.com" not in repr(health)


def test_send_readiness_private_api_disabled_only_matters_for_private_api() -> None:
    health = BlueBubblesHealth(
        reachable=True, authenticated=True, private_api=False, helper_connected=False
    )

    assert "Private API is disabled" in describe_send_readiness(health, "private-api")
    assert describe_send_readiness(health, "apple-script") == ""


def test_send_readiness_flags_disconnected_helper() -> None:
    health = BlueBubblesHealth(
        reachable=True, authenticated=True, private_api=True, helper_connected=False
    )

    assert "helper is not connected" in describe_send_readiness(health, "private-api")


def test_send_readiness_healthy_server_reports_nothing() -> None:
    health = BlueBubblesHealth(
        reachable=True,
        authenticated=True,
        private_api=True,
        helper_connected=True,
        imessage_signed_in=True,
    )

    assert describe_send_readiness(health, "private-api") == ""


# ---------------------------------------------------------------------------
# Webhook registration verification
# ---------------------------------------------------------------------------


def _webhook_list_handler(webhooks: list[dict[str, Any]]) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "message": "Success", "data": webhooks})

    return handler


async def test_verify_accepts_the_url_we_would_register() -> None:
    """Round-trip: the URL built for registration is the URL the check looks for."""
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    with _mock_bb_http(
        _webhook_list_handler([{"id": 1, "url": expected, "events": ["new-message"]}])
    ):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert check.ok
    assert check.listed


async def test_verify_reports_missing_registration() -> None:
    """The failure that silently kills inbound: registration never landed."""
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    with _mock_bb_http(_webhook_list_handler([])):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert not check.ok
    assert check.listed  # we asked and got an answer: safe to repair
    assert "no webhooks are registered" in check.detail


async def test_verify_reports_a_registration_for_a_different_base_url() -> None:
    """A base-URL change leaves the old registration pointing at a dead URL."""
    expected = build_webhook_url("https://new.example", _PASSWORD)
    stale = build_webhook_url("https://old.example", _PASSWORD)
    with _mock_bb_http(_webhook_list_handler([{"id": 7, "url": stale, "events": ["new-message"]}])):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert not check.ok
    assert "no webhook registered for https://new.example" in check.detail
    assert check.registered_endpoints == ("https://old.example/api/webhooks/bluebubbles",)


async def test_verify_detects_a_stale_token_on_the_right_endpoint() -> None:
    """Same URL, token from the previous password: delivered, then rejected at the door."""
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    stale = build_webhook_url("https://clawbolt.example", "the-old-password")
    with _mock_bb_http(_webhook_list_handler([{"id": 3, "url": stale, "events": ["new-message"]}])):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert not check.ok
    assert "token does not match" in check.detail


async def test_verify_rejects_a_registration_without_the_new_message_event() -> None:
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    with _mock_bb_http(
        _webhook_list_handler([{"id": 4, "url": expected, "events": ["typing-indicator"]}])
    ):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert not check.ok
    assert "typing-indicator" in check.detail


@pytest.mark.parametrize(
    "events",
    [
        '["new-message"]',
        [{"label": "New Message", "value": "new-message"}],
        ["new-message", "updated-message"],
    ],
    ids=["json-string", "label-value-objects", "plain-list"],
)
async def test_verify_parses_every_event_encoding(events: Any) -> None:
    """BlueBubbles has shipped the events field in all three shapes."""
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    with _mock_bb_http(_webhook_list_handler([{"id": 5, "url": expected, "events": events}])):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert check.ok


async def test_verify_accepts_the_all_events_wildcard() -> None:
    """BlueBubbles ships "All Events" as ``*`` and its dispatcher honours it.

    An operator who repairs a broken registration by hand picks that option, and
    the webhook does deliver new-message. Calling it a mismatch would report a
    working bridge as broken and trigger a repair the premium monitor emails about.
    """
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    with _mock_bb_http(_webhook_list_handler([{"id": 8, "url": expected, "events": ["*"]}])):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert check.ok


async def test_list_webhooks_returns_none_for_a_scalar_json_body() -> None:
    """A 200 carrying ``null`` used to raise ``AttributeError`` out of the helper.

    ``verify_webhook_registration`` has no handler of its own, so the escape
    reached the health path rather than reporting "unknown".
    """
    with _mock_bb_http(
        lambda _req: httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    ):
        assert await list_bluebubbles_webhooks("http://bb.example", "pw") is None
        check = await verify_webhook_registration("http://bb.example", "https://x.example", "pw")

    assert not check.listed


async def test_verify_passes_when_the_event_format_is_unrecognized() -> None:
    """Unknown is not failure: a shape we cannot parse must not read as an outage."""
    expected = build_webhook_url("https://clawbolt.example", _PASSWORD)
    with _mock_bb_http(_webhook_list_handler([{"id": 6, "url": expected, "events": {"weird": 1}}])):
        check = await verify_webhook_registration("http://bb.example", expected, _PASSWORD)

    assert check.ok


async def test_verify_marks_an_unanswered_list_as_not_listed() -> None:
    """``listed=False`` tells the caller "unknown", so it does not try to repair."""
    with _mock_bb_http(lambda _req: httpx.Response(500, text="boom")):
        check = await verify_webhook_registration("http://bb.example", "https://x.example", "pw")

    assert not check.ok
    assert not check.listed
    assert "could not list" in check.detail


async def test_list_webhooks_returns_none_when_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _mock_bb_http(handler):
        assert await list_bluebubbles_webhooks("http://bb.example", "pw") is None


async def test_list_webhooks_distinguishes_empty_from_unreachable() -> None:
    with _mock_bb_http(_webhook_list_handler([])):
        assert await list_bluebubbles_webhooks("http://bb.example", "pw") == []


def test_build_webhook_url_uses_the_derived_token_not_the_password() -> None:
    url = build_webhook_url("https://clawbolt.example/", _PASSWORD)

    assert url.startswith("https://clawbolt.example/api/webhooks/bluebubbles?token=")
    assert _derive_webhook_token(_PASSWORD) in url
    assert _PASSWORD not in url


# ---------------------------------------------------------------------------
# Channel integration
# ---------------------------------------------------------------------------


async def test_channel_check_health_stores_the_last_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bluebubbles_server_url", _SERVER)
    monkeypatch.setattr(settings, "bluebubbles_password", _PASSWORD)
    channel = BlueBubblesChannel()
    assert channel.last_health is None

    with _mock_bb_http(_info_response(server_version="1.9.8", detected_icloud="a@example.com")):
        health = await channel.check_health()

    assert health.ok
    assert channel.last_health is health


async def test_channel_reachability_is_false_when_the_password_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the green-light-on-401 bug, at the channel level."""
    monkeypatch.setattr(settings, "bluebubbles_server_url", _SERVER)
    monkeypatch.setattr(settings, "bluebubbles_password", "wrong")
    channel = BlueBubblesChannel()

    with _mock_bb_http(lambda _req: httpx.Response(401, text="Unauthorized")):
        assert await channel._check_server_reachable() is False
