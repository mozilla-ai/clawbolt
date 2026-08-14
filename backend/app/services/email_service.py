"""Transactional email sender backed by SMTP (AWS SES in production).

Used for waitlist approval notifications today; structured so additional
templates can be added as plain functions over a shared `_send` helper.

Design:

- When `SMTP_HOST` is empty the sender is a no-op so dev/local and CI work
  without SES credentials. Callers do not need to gate on configuration.
- SMTP I/O is synchronous (stdlib `smtplib`) and runs in a thread executor so
  FastAPI handlers stay async-clean.
- Failures never raise to the caller. The waitlist approval is the source of
  truth in the database; a transient SES outage must not undo the DB write
  or surface as a 500 to the admin. Errors are logged for follow-up.
- The last attempt's outcome is kept in memory and exposed via
  `transport_status()`. Returning a bare `False` meant the admin Monitoring tab
  could only say "not sent", leaving the operator to go read container logs to
  learn whether the cause was a typo'd recipient, a rejected password, or a
  blocked port. `diagnose_transport()` answers that on demand from inside the
  running container.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import smtplib
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING

from backend.app.config import settings
from backend.app.models import WAITLIST_NAME_DEFAULT
from backend.app.web_paths import LOGIN_PATH

if TYPE_CHECKING:
    # Runtime import would be circular: both alert modules import this one for
    # the shared ``_send`` transport.
    from backend.app.services.admin_alerts import AlertSummary
    from backend.app.services.health_monitor import HealthTransition

logger = logging.getLogger(__name__)

# Operator email styling. Deliberately plainer than the user-facing templates
# below: these are read on a phone at 6am while something is broken, so
# legibility beats brand. Monospace for anything copy-pasted into a terminal.
_OPS_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif"
_OPS_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
_OPS_DOWN = "#B42318"
_OPS_UP = "#067647"
_OPS_TEXT = "#2D2A26"
_OPS_MUTED = "#7A746C"
_OPS_BORDER = "#E3DFD9"


def _is_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from_email)


def _socket_timeout() -> float:
    return float(max(1, settings.smtp_timeout_seconds))


def _connect(timeout: float) -> smtplib.SMTP:
    """Open an authenticated, TLS-wrapped SMTP session. Caller closes it."""
    context = ssl.create_default_context()
    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
    except Exception:
        smtp.close()
        raise
    return smtp


def _send_sync(msg: EmailMessage) -> None:
    """Blocking SMTP send. Runs inside `asyncio.to_thread`."""
    smtp = _connect(_socket_timeout())
    try:
        smtp.send_message(msg)
    finally:
        smtp.close()


def _explain_failure(exc: BaseException, elapsed: float) -> str:
    """Turn a transport exception into something an operator can act on.

    The generic ``TimeoutError: timed out`` that smtplib raises is the least
    informative rendering of the most common failure. Naming the endpoint, the
    elapsed time, and the likely cause is the difference between a one-line
    answer and an afternoon in the logs. Host and port are infrastructure, not
    PII, so they are safe to name; the recipient is never included.
    """
    where = f"{settings.smtp_host}:{settings.smtp_port}"
    # ``socket.timeout`` is an alias of ``TimeoutError`` on 3.10+, so smtplib's
    # connect and read timeouts both land here.
    if isinstance(exc, TimeoutError):
        return (
            f"Timed out after {elapsed:.0f}s talking to {where}. Nothing answered, which "
            "usually means outbound SMTP is blocked rather than misconfigured: the platform "
            "firewalls the port, or the plan does not permit SMTP at all. Run the delivery "
            "diagnostic to see which ports this container can reach."
        )
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            f"{where} rejected the credentials ({exc.smtp_code}). Check SMTP_USERNAME / "
            "SMTP_PASSWORD; SES SMTP credentials are not the same as AWS API keys."
        )
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (
            f"{where} refused the sender address {settings.smtp_from_email!r} "
            f"({exc.smtp_code}). It likely is not a verified identity."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return (
            f"{where} refused the recipient. If the account is still in the SES sandbox, "
            "every recipient must be verified individually."
        )
    if isinstance(exc, ssl.SSLError):
        return (
            f"TLS handshake with {where} failed: {exc}. Port 587 expects STARTTLS on a "
            "plaintext connection; port 465 expects TLS from the first byte and is not "
            "supported here."
        )
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"Could not reach {where}: {exc.strerror or exc} (errno {exc.errno})."
    return f"{type(exc).__name__} talking to {where}: {exc}"


@dataclass(frozen=True)
class TransportStatus:
    """What the SMTP transport did the last time anything tried to use it."""

    configured: bool
    host: str
    port: int
    timeout_seconds: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str
    last_error_at: datetime | None


# Last-attempt state, process-local and best-effort. Two admins clicking at once
# can interleave, which is acceptable for an operations readout; the durable
# record is the log line.
_last_attempt_at: datetime | None = None
_last_success_at: datetime | None = None
_last_error: str = ""
_last_error_at: datetime | None = None


def transport_status() -> TransportStatus:
    """Current transport configuration plus the last attempt's outcome."""
    return TransportStatus(
        configured=_is_configured(),
        host=settings.smtp_host,
        port=settings.smtp_port,
        timeout_seconds=int(_socket_timeout()),
        last_attempt_at=_last_attempt_at,
        last_success_at=_last_success_at,
        last_error=_last_error,
        last_error_at=_last_error_at,
    )


def _record_success() -> None:
    global _last_attempt_at, _last_success_at, _last_error, _last_error_at
    _last_attempt_at = _last_success_at = datetime.now(UTC)
    _last_error = ""
    _last_error_at = None


def _record_failure(detail: str) -> None:
    global _last_attempt_at, _last_error, _last_error_at
    _last_attempt_at = _last_error_at = datetime.now(UTC)
    _last_error = detail


async def _send(msg: EmailMessage) -> bool:
    """Best-effort send. Returns True on success, False otherwise.

    Recipient addresses are kept out of INFO/ERROR logs (PII discipline,
    issue #1082); the admin audit log row already records who was emailed.

    Bounded by twice the socket timeout. ``socket.create_connection`` tries
    every address the SMTP host resolves to, each with its own timeout, so a
    blocked port on a three-A-record host would otherwise hold the caller for
    three full timeouts. Exceeding the budget abandons the thread rather than
    killing it: ``to_thread`` cannot be cancelled, so the socket stays open
    until the OS gives up, but the request no longer waits for it.
    """
    subject = msg["Subject"]
    if not _is_configured():
        logger.info("smtp_not_configured: skipping email subject=%r", subject)
        _record_failure("SMTP is not configured: SMTP_HOST and SMTP_FROM_EMAIL are unset.")
        return False

    budget = _socket_timeout() * 2
    started = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.to_thread(_send_sync, msg), timeout=budget)
    except TimeoutError:
        # Budget expired. The send thread keeps running, detached, and its
        # eventual outcome is discarded; the caller gets the transport timeout.
        detail = _explain_failure(TimeoutError(), time.monotonic() - started)
        logger.error("smtp_send_failed: subject=%r %s", subject, detail)
        _record_failure(detail)
        return False
    except Exception as exc:
        detail = _explain_failure(exc, time.monotonic() - started)
        logger.exception("smtp_send_failed: subject=%r %s", subject, detail)
        _record_failure(detail)
        return False
    logger.debug("smtp_send_ok: to=%s subject=%r", msg["To"], subject)
    _record_success()
    return True


# ---------------------------------------------------------------------------
# Delivery diagnostics
# ---------------------------------------------------------------------------

# Ports worth reporting on. 587 (STARTTLS) and 465 (implicit TLS) are the
# standard submission pair; 2587 and 2465 are the SES alternates that exist
# precisely because networks block the standard ones; 25 distinguishes "this
# host blocks submission" from "this host blocks SMTP entirely".
_DIAGNOSTIC_PORTS = (25, 465, 587, 2465, 2587)

# Short on purpose: this is a reachability question, and every candidate is
# probed concurrently. A port that has not completed a TCP handshake in 5s is
# not one to point production email at.
_PORT_PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class PortCheck:
    port: int
    reachable: bool
    detail: str


async def _check_port(host: str, port: int) -> PortCheck:
    """TCP-connect only. Proves reachability without touching the mail state."""
    started = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_PORT_PROBE_TIMEOUT
        )
    except TimeoutError:
        return PortCheck(
            port=port,
            reachable=False,
            detail=f"no response within {_PORT_PROBE_TIMEOUT:.0f}s (connection dropped, not refused)",
        )
    except OSError as exc:
        return PortCheck(port=port, reachable=False, detail=exc.strerror or str(exc))
    except Exception as exc:
        return PortCheck(port=port, reachable=False, detail=f"{type(exc).__name__}: {exc}")
    elapsed_ms = (time.monotonic() - started) * 1000
    writer.close()
    # The peer hanging up on an unfinished SMTP session is not a finding: the
    # handshake already answered the reachability question.
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return PortCheck(port=port, reachable=True, detail=f"connected in {elapsed_ms:.0f}ms")


def _handshake_sync() -> str:
    """EHLO + STARTTLS + login against the configured port. No message sent."""
    smtp = _connect(_socket_timeout())
    try:
        code, _ = smtp.noop()
    finally:
        smtp.close()
    authenticated = " and accepted the credentials" if settings.smtp_username else ""
    return f"EHLO, STARTTLS and NOOP ({code}) succeeded{authenticated}"


def _conclusion(status: TransportStatus, ports: list[PortCheck], handshake_error: str) -> str:
    """One operator-facing sentence: what is wrong and what to do about it."""
    if not status.configured:
        return (
            "SMTP is not configured, so nothing is emailed. Set SMTP_HOST and "
            "SMTP_FROM_EMAIL, plus ALERT_EMAIL or ADMIN_EMAIL."
        )
    if not handshake_error:
        return (
            f"Email works from this container: {status.host}:{status.port} completed a full "
            "authenticated handshake. If alerts are still not arriving, the failure is "
            "downstream of the transport: an unverified sender identity, the SES sandbox "
            "restricting recipients, or the mail landing in spam."
        )
    reachable = [p for p in ports if p.reachable]
    configured_reachable = any(p.reachable for p in ports if p.port == status.port)
    if not reachable:
        return (
            f"No SMTP port on {status.host} is reachable from this container, so this is a "
            "network-level block rather than a mail misconfiguration. Railway permits "
            "outbound SMTP only on Pro and above; other hosts commonly block submission "
            "ports outright. Either get the platform to allow it, or move alerting to an "
            "HTTPS email API."
        )
    if not configured_reachable:
        alternates = ", ".join(str(p.port) for p in reachable)
        return (
            f"Port {status.port} is blocked from this container but {alternates} "
            f"{'is' if len(reachable) == 1 else 'are'} reachable. SES offers 2587 as a "
            "STARTTLS alternate for exactly this case: set SMTP_PORT to a reachable port. "
            "Note that 465 and 2465 expect TLS from the first byte and are not supported."
        )
    return (
        f"{status.host}:{status.port} accepts a TCP connection, so the network path is open "
        f"and the failure is in the SMTP session itself: {handshake_error}"
    )


async def diagnose_transport() -> dict[str, object]:
    """Probe the email path from inside the container and explain the result.

    Exists because the alert transport is exercised only when something is
    already broken, and its most likely failure (a silently dropped connection)
    is indistinguishable in the logs from a dozen benign misconfigurations. The
    port sweep separates "the network will not let us out" from "the mail server
    said no", which are different people's problems.
    """
    status = transport_status()
    if not status.configured:
        return {
            "configured": False,
            "host": "",
            "port": status.port,
            "ports": [],
            "handshake_ok": False,
            "handshake_detail": "Not attempted: SMTP is not configured.",
            "conclusion": _conclusion(status, [], "not configured"),
        }

    candidates = sorted({*_DIAGNOSTIC_PORTS, status.port})
    ports = list(await asyncio.gather(*(_check_port(status.host, port) for port in candidates)))

    handshake_detail = ""
    handshake_error = ""
    try:
        handshake_detail = await asyncio.wait_for(
            asyncio.to_thread(_handshake_sync), timeout=_socket_timeout() * 2
        )
    except TimeoutError:
        handshake_error = _explain_failure(TimeoutError(), _socket_timeout() * 2)
        handshake_detail = handshake_error
    except Exception as exc:
        handshake_error = _explain_failure(exc, 0.0)
        handshake_detail = handshake_error

    return {
        "configured": True,
        "host": status.host,
        "port": status.port,
        "ports": [{"port": p.port, "reachable": p.reachable, "detail": p.detail} for p in ports],
        "handshake_ok": not handshake_error,
        "handshake_detail": handshake_detail,
        "conclusion": _conclusion(status, ports, handshake_error),
    }


def reset_for_tests() -> None:
    """Clear last-attempt state so one test's failure does not leak into another."""
    global _last_attempt_at, _last_success_at, _last_error, _last_error_at
    _last_attempt_at = _last_success_at = _last_error_at = None
    _last_error = ""


def _greeting_name(raw: str) -> str:
    """Return a display-safe first-name greeting for the approval email.

    Picks the first whitespace-separated token so a full name like
    ``Jane Doe`` greets as ``Jane`` (more personal, matches how trades pros
    sign off SMS replies). Falls back to a neutral ``there`` when the stored
    value is empty or the placeholder ``user`` used by the p029 backfill, so
    legacy entries still read naturally.
    """
    cleaned = raw.strip()
    if not cleaned or cleaned.lower() == WAITLIST_NAME_DEFAULT:
        return "there"
    first = cleaned.split()[0]
    return first[:64]


def _waitlist_approved_message(to_email: str, name: str) -> EmailMessage:
    """Build the multipart approval email.

    Visual design follows DESIGN.md: amber primary (#B8720E), warm-black text
    (#2D2A26) on cream background (#F6F5F3), Outfit display heading with a
    system-font fallback for clients that strip <link>-based webfonts.

    Email client compatibility notes:
    - The hero banner sets a solid amber background first, then layers a
      radial gradient via background-image. Modern clients (Apple Mail,
      Gmail, Outlook 365) render the gradient; legacy Outlook falls back
      to the solid amber, which still reads on-brand.
    - Layout uses tables instead of flexbox so Outlook (Word rendering
      engine) does not collapse the column. Inline styles only.
    - No remote font/asset loads. Pulling Outfit/DM Sans from Google Fonts
      would expose recipient IP and open timestamp to a third party every
      time the email is read. The font stack lists the brand fonts first
      so users who already have them locally still get them; otherwise
      clients fall back through the system stack.
    """
    sign_in_url = f"{settings.app_base_url.rstrip('/')}{LOGIN_PATH}"
    subject = "You're in. Welcome to Clawbolt."
    greeting = _greeting_name(name)

    text_body = (
        f"Hi {greeting},\n\n"
        "You're off the Clawbolt waitlist. Your account is ready, sign in whenever you're ready.\n\n"
        f"Sign in: {sign_in_url}\n\n"
        "Clawbolt handles estimates, scheduling, and client follow-ups so you can focus on the "
        "work that matters.\n\n"
        "Questions? Just reply to this email.\n\n"
        "Thanks,\n"
        "The Clawbolt team\n"
    )

    # DESIGN.md tokens used below:
    #   primary           #B8720E
    #   primary-hover     #9A5F0B
    #   foreground        #2D2A26
    #   muted-foreground  #7A746C
    #   background        #F6F5F3
    #   card              #FEFEFE
    #   border            #E3DFD9
    font_stack = (
        "'Outfit', 'DM Sans', -apple-system, BlinkMacSystemFont, "
        "'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
    )

    html_body = f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light only">
  <meta name="supported-color-schemes" content="light only">
  <title>{subject}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #F6F5F3; font-family: {font_stack}; color: #2D2A26;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #F6F5F3;">
    <tr>
      <td align="center" style="padding: 32px 16px;">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="max-width: 560px; width: 100%; background-color: #FEFEFE; border: 1px solid #E3DFD9; border-radius: 14px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(45,42,38,0.07), 0 2px 4px -2px rgba(45,42,38,0.05);">
          <tr>
            <td style="background-color: #B8720E; background-image: radial-gradient(circle at 50% 30%, #D4940F 0%, #B8720E 35%, #9A5F0B 65%, #7D4D09 100%); padding: 44px 32px 36px; text-align: center;">
              <p style="margin: 0 0 12px; font-family: {font_stack}; font-size: 11px; font-weight: 500; letter-spacing: 3px; text-transform: uppercase; color: rgba(255,255,255,0.8);">
                AI for the trades
              </p>
              <h1 style="margin: 0; font-family: {font_stack}; font-size: 32px; font-weight: 700; line-height: 1.15; color: #FFFFFF; letter-spacing: -0.01em;">
                You're in.
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding: 36px 32px 8px;">
              <p style="margin: 0 0 16px; font-family: {font_stack}; font-size: 15px; line-height: 1.6; color: #2D2A26;">
                Hi {html.escape(greeting)},
              </p>
              <p style="margin: 0 0 24px; font-family: {font_stack}; font-size: 15px; line-height: 1.6; color: #2D2A26;">
                You're off the Clawbolt waitlist. Your account is ready, sign in whenever you're ready.
              </p>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin: 4px 0 28px;">
                <tr>
                  <td style="background-color: #B8720E; border-radius: 8px;">
                    <a href="{sign_in_url}"
                       style="display: inline-block; padding: 14px 28px; font-family: {font_stack}; font-size: 15px; font-weight: 600; color: #FFFFFF; text-decoration: none; border-radius: 8px;">
                      Sign in to Clawbolt
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin: 0 0 24px; font-family: {font_stack}; font-size: 15px; line-height: 1.6; color: #2D2A26;">
                Clawbolt handles estimates, scheduling, and client follow-ups so you can focus on the work that matters.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding: 0 32px 32px;">
              <hr style="border: 0; border-top: 1px solid #E3DFD9; margin: 0 0 20px;">
              <p style="margin: 0 0 8px; font-family: {font_stack}; font-size: 13px; line-height: 1.5; color: #7A746C;">
                Button not working? Paste this link into your browser:
              </p>
              <p style="margin: 0 0 20px; font-family: {font_stack}; font-size: 13px; line-height: 1.5; color: #7A746C; word-break: break-all;">
                {sign_in_url}
              </p>
              <p style="margin: 0 0 6px; font-family: {font_stack}; font-size: 13px; line-height: 1.5; color: #7A746C;">
                Questions? Just reply to this email.
              </p>
              <p style="margin: 0; font-family: {font_stack}; font-size: 13px; line-height: 1.5; color: #7A746C;">
                Thanks,<br>The Clawbolt team
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    msg = EmailMessage()
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


async def send_waitlist_approved(to_email: str, name: str = "") -> bool:
    """Notify a user that their waitlist request was approved.

    ``name`` is the value captured at signup (empty / ``user`` for legacy rows
    that predate the prompt); the template normalizes it into a first-name
    greeting and falls back to a neutral ``there`` when no real name is on
    file.

    Best-effort: returns False on misconfiguration or send failure rather
    than raising, so the caller (the admin approve endpoint) can keep its
    response semantics tied to the DB write.

    The send is awaited rather than fire-and-forget so the caller can record
    the outcome in the audit log. Worst case: an unresponsive SES holds the
    admin's request for the SMTP connect timeout (15 s) before the call
    returns False.
    """
    return await _send(_waitlist_approved_message(to_email, name))


# ---------------------------------------------------------------------------
# Operator alerting
# ---------------------------------------------------------------------------


def _alert_subject(alerts: Sequence[AlertSummary]) -> str:
    """Front-load the subject with what broke, so triage happens in the inbox."""
    if len(alerts) == 1:
        alert = alerts[0]
        occurrences = f" (x{alert.count})" if alert.count > 1 else ""
        return f"[clawbolt] {alert.title}{occurrences}"
    total = sum(a.count for a in alerts)
    return f"[clawbolt] {len(alerts)} error groups, {total} occurrences"


def _admin_alert_message(
    to_email: str, alerts: Sequence[AlertSummary], dropped: int
) -> EmailMessage:
    """Build the grouped error-alert email."""
    subject = _alert_subject(alerts)

    text_parts: list[str] = []
    html_parts: list[str] = []
    for alert in alerts:
        occurrences = f"{alert.count} occurrence{'s' if alert.count != 1 else ''}"
        window = (
            f"{alert.first_seen:%Y-%m-%d %H:%M:%S} to {alert.last_seen:%H:%M:%S} UTC"
            if alert.count > 1
            else f"{alert.first_seen:%Y-%m-%d %H:%M:%S} UTC"
        )
        text_parts.append(
            f"{alert.level} {alert.logger_name}\n"
            f"{alert.title}\n"
            f"{occurrences}, {window}\n"
            f"request_id: {alert.request_id}\n"
            + (f"\n{alert.traceback_text}\n" if alert.traceback_text else "\n")
        )
        traceback_block = (
            f'<pre style="margin: 12px 0 0; padding: 12px; background-color: #F6F5F3; '
            f"border: 1px solid {_OPS_BORDER}; border-radius: 6px; font-family: {_OPS_MONO}; "
            f"font-size: 12px; line-height: 1.45; overflow-x: auto; white-space: pre-wrap; "
            f'word-break: break-word; color: {_OPS_TEXT};">{html.escape(alert.traceback_text)}</pre>'
            if alert.traceback_text
            else ""
        )
        html_parts.append(
            f"""
        <tr>
          <td style="padding: 20px 0; border-bottom: 1px solid {_OPS_BORDER};">
            <p style="margin: 0 0 6px; font-family: {_OPS_FONT}; font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: {_OPS_DOWN};">
              {html.escape(alert.level)} &middot; {html.escape(alert.logger_name)}
            </p>
            <p style="margin: 0 0 8px; font-family: {_OPS_MONO}; font-size: 14px; line-height: 1.5; color: {_OPS_TEXT}; word-break: break-word;">
              {html.escape(alert.title)}
            </p>
            <p style="margin: 0; font-family: {_OPS_FONT}; font-size: 12px; line-height: 1.5; color: {_OPS_MUTED};">
              {html.escape(occurrences)} &middot; {html.escape(window)} &middot; request_id
              <span style="font-family: {_OPS_MONO};">{html.escape(alert.request_id)}</span>
            </p>
            {traceback_block}
          </td>
        </tr>"""
        )

    dropped_note = (
        f"\n{dropped} additional distinct error group(s) were dropped during this window "
        f"(too many unique errors at once).\n"
        if dropped
        else ""
    )
    dropped_html = (
        f'<p style="margin: 16px 0 0; font-family: {_OPS_FONT}; font-size: 12px; color: {_OPS_DOWN};">'
        f"{dropped} additional distinct error group(s) were dropped during this window."
        f"</p>"
        if dropped
        else ""
    )

    text_body = (
        "Clawbolt application errors\n"
        "===========================\n\n" + "\n".join(text_parts) + dropped_note
    )

    html_body = f"""\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(subject)}</title></head>
<body style="margin: 0; padding: 0; background-color: #F6F5F3; font-family: {_OPS_FONT}; color: {_OPS_TEXT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center" style="padding: 24px 12px;">
        <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="max-width: 640px; width: 100%; background-color: #FEFEFE; border: 1px solid {_OPS_BORDER}; border-radius: 10px;">
          <tr>
            <td style="padding: 20px 24px 4px;">
              <h1 style="margin: 0; font-family: {_OPS_FONT}; font-size: 18px; font-weight: 700; color: {_OPS_TEXT};">
                Application errors
              </h1>
              <p style="margin: 6px 0 0; font-family: {_OPS_FONT}; font-size: 13px; color: {_OPS_MUTED};">
                Grouped by logger and exception type. Repeats within the dedupe window are counted, not resent.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding: 0 24px 20px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {"".join(html_parts)}
              </table>
              {dropped_html}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    msg = EmailMessage()
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


async def send_admin_alert(to_email: str, alerts: Sequence[AlertSummary], dropped: int = 0) -> bool:
    """Email a batch of grouped application errors to the operator.

    Best-effort like every sender here. Returns False on misconfiguration or
    send failure; the caller decides whether to retry (``admin_alerts`` does not
    start the dedupe cooldown on failure, so the next occurrence tries again).
    """
    if not alerts:
        return False
    return await _send(_admin_alert_message(to_email, alerts, dropped))


def _health_subject(transitions: Sequence[HealthTransition]) -> str:
    down = [t for t in transitions if t.status == "down"]
    up = [t for t in transitions if t.status == "up"]
    if len(transitions) == 1:
        t = transitions[0]
        word = "DOWN" if t.status == "down" else "RECOVERED"
        return f"[clawbolt] {word}: {t.label}"
    if down and not up:
        return f"[clawbolt] DOWN: {len(down)} systems"
    if up and not down:
        return f"[clawbolt] RECOVERED: {len(up)} systems"
    return f"[clawbolt] {len(down)} down, {len(up)} recovered"


def _health_alert_message(to_email: str, transitions: Sequence[HealthTransition]) -> EmailMessage:
    """Build the health-transition email."""
    subject = _health_subject(transitions)

    text_parts: list[str] = []
    html_parts: list[str] = []
    for t in transitions:
        word = "DOWN" if t.status == "down" else "RECOVERED"
        color = _OPS_DOWN if t.status == "down" else _OPS_UP
        failures = (
            f" after {t.consecutive_failures} consecutive failed checks"
            if t.status == "down"
            else ""
        )
        text_parts.append(
            f"[{word}] {t.label}{failures}\n"
            f"  key: {t.key}\n"
            f"  at: {t.since:%Y-%m-%d %H:%M:%S} UTC\n"
            + (f"  detail: {t.detail}\n" if t.detail else "")
        )
        detail_html = (
            f'<p style="margin: 6px 0 0; font-family: {_OPS_MONO}; font-size: 12px; '
            f'line-height: 1.5; color: {_OPS_TEXT}; word-break: break-word;">{html.escape(t.detail)}</p>'
            if t.detail
            else ""
        )
        html_parts.append(
            f"""
        <tr>
          <td style="padding: 16px 0; border-bottom: 1px solid {_OPS_BORDER};">
            <p style="margin: 0 0 4px; font-family: {_OPS_FONT}; font-size: 15px; font-weight: 700; color: {color};">
              {word} &middot; {html.escape(t.label)}
            </p>
            <p style="margin: 0; font-family: {_OPS_FONT}; font-size: 12px; color: {_OPS_MUTED};">
              {html.escape(t.since.strftime("%Y-%m-%d %H:%M:%S"))} UTC{html.escape(failures)}
              &middot; <span style="font-family: {_OPS_MONO};">{html.escape(t.key)}</span>
            </p>
            {detail_html}
          </td>
        </tr>"""
        )

    text_body = (
        "Clawbolt health status change\n"
        "=============================\n\n" + "\n".join(text_parts) + "\n"
        "Steady-state failures are not resent. The next email for a system "
        "arrives when it recovers.\n"
    )

    html_body = f"""\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(subject)}</title></head>
<body style="margin: 0; padding: 0; background-color: #F6F5F3; font-family: {_OPS_FONT}; color: {_OPS_TEXT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center" style="padding: 24px 12px;">
        <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="max-width: 640px; width: 100%; background-color: #FEFEFE; border: 1px solid {_OPS_BORDER}; border-radius: 10px;">
          <tr>
            <td style="padding: 20px 24px 4px;">
              <h1 style="margin: 0; font-family: {_OPS_FONT}; font-size: 18px; font-weight: 700; color: {_OPS_TEXT};">
                Health status change
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding: 0 24px 8px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {"".join(html_parts)}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding: 4px 24px 20px;">
              <p style="margin: 0; font-family: {_OPS_FONT}; font-size: 12px; line-height: 1.5; color: {_OPS_MUTED};">
                Steady-state failures are not resent. The next email for a system arrives when it recovers.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    msg = EmailMessage()
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


async def send_health_alert(to_email: str, transitions: Sequence[HealthTransition]) -> bool:
    """Email dependency health transitions (down / recovered) to the operator."""
    if not transitions:
        return False
    return await _send(_health_alert_message(to_email, transitions))


def _repair_notice_message(to_email: str, label: str, problem: str, outcome: str) -> EmailMessage:
    """Build the "something was broken and the app fixed it" email."""
    subject = f"[clawbolt] REPAIRED: {label}"
    text_body = (
        "Clawbolt self-repair\n"
        "====================\n\n"
        f"{label}\n\n"
        f"  problem: {problem}\n"
        f"  action:  {outcome}\n\n"
        "This is reported separately from the health alerts because the repair "
        "resolves the failure before the consecutive-failure threshold is met, "
        "so no DOWN alert would ever be sent for it.\n"
    )

    html_body = f"""\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(subject)}</title></head>
<body style="margin: 0; padding: 0; background-color: #F6F5F3; font-family: {_OPS_FONT}; color: {_OPS_TEXT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center" style="padding: 24px 12px;">
        <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="max-width: 640px; width: 100%; background-color: #FEFEFE; border: 1px solid {_OPS_BORDER}; border-radius: 10px;">
          <tr>
            <td style="padding: 20px 24px 4px;">
              <h1 style="margin: 0; font-family: {_OPS_FONT}; font-size: 18px; font-weight: 700; color: {_OPS_TEXT};">
                Self-repair &middot; {html.escape(label)}
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding: 8px 24px;">
              <p style="margin: 0 0 4px; font-family: {_OPS_FONT}; font-size: 13px; font-weight: 700; color: {_OPS_DOWN};">Problem</p>
              <p style="margin: 0 0 14px; font-family: {_OPS_MONO}; font-size: 12px; line-height: 1.5; color: {_OPS_TEXT}; word-break: break-word;">{html.escape(problem)}</p>
              <p style="margin: 0 0 4px; font-family: {_OPS_FONT}; font-size: 13px; font-weight: 700; color: {_OPS_UP};">Action taken</p>
              <p style="margin: 0; font-family: {_OPS_MONO}; font-size: 12px; line-height: 1.5; color: {_OPS_TEXT}; word-break: break-word;">{html.escape(outcome)}</p>
            </td>
          </tr>
          <tr>
            <td style="padding: 12px 24px 20px;">
              <p style="margin: 0; font-family: {_OPS_FONT}; font-size: 12px; line-height: 1.5; color: {_OPS_MUTED};">
                Sent separately from health alerts: the repair resolves the failure before the
                consecutive-failure threshold is met, so no DOWN alert would ever fire for it.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    msg = EmailMessage()
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


async def send_repair_notice(to_email: str, label: str, problem: str, outcome: str) -> bool:
    """Email the operator that a dependency was found broken and repaired."""
    if not to_email:
        return False
    return await _send(_repair_notice_message(to_email, label, problem, outcome))
