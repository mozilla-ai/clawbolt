"""Hook for reporting failed tool calls to an out-of-band consumer.

Mirrors the module-level setter pattern used by ``set_pipeline_override`` in
``backend.app.agent.router`` and ``install_llm_payload_capture``. The agent
loop calls :func:`report_tool_failure` at every tool failure site; with no
handler installed the call is a branch and a return, so single-user
deployments and CI pay nothing.

Why a hook rather than the existing ERROR-log alerting: only the ``INTERNAL``
case (a tool raising) logs at ERROR, and it does so through one template,
``"Tool call failed: %s"``. ``admin_alerts`` fingerprints on the *template*,
so every crashing tool in the system collapses into a single alert group whose
formatted message names only the most recent one. ``SERVICE`` and ``AUTH``
failures, where an integration is down or a token has been revoked, log at
WARNING and never reach the alerting path at all. Per-tool attribution and
those two kinds are the new information.

The handler runs inline in a user-facing turn, so it must not block. Doing I/O
here adds latency to a reply somebody is waiting on.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Bumped when fields are added, so a handler compiled against an older build
# can tell what it is looking at. Handlers should read fields by name and
# tolerate unknown ones.
TOOL_FAILURE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ToolFailurePayload:
    """Versioned snapshot of one failed tool call.

    ``args`` is the validated argument dict passed to the tool and ``result_text``
    is what the model was shown. Both can carry user data, so a consumer is
    responsible for gating them on consent and redacting before they leave the
    process. The remaining fields are safe to aggregate for any user.
    """

    schema_version: int
    user_id: str
    tool_name: str
    # ``ToolErrorKind`` value. Kept as a plain string so the hook does not drag
    # the tools package into every consumer's import graph.
    error_kind: str
    args: dict[str, Any] = field(default_factory=dict)
    result_text: str = ""


ToolFailureHandler = Callable[[ToolFailurePayload], Awaitable[None]]

_handler: ToolFailureHandler | None = None


def set_tool_failure_handler(handler: ToolFailureHandler | None) -> None:
    """Install (or clear, with ``None``) the process-wide failure handler."""
    global _handler
    _handler = handler


def get_tool_failure_handler() -> ToolFailureHandler | None:
    """Return the installed handler, or None. For tests and diagnostics."""
    return _handler


async def report_tool_failure(payload: ToolFailurePayload) -> None:
    """Dispatch one tool failure. Never raises, never blocks meaningfully.

    Exceptions are swallowed at DEBUG rather than ERROR on purpose: this runs
    under the alerting stack, and logging an ERROR from the path that feeds
    alerting is how a reporting bug turns into an alert storm about itself.
    """
    handler = _handler
    if handler is None:
        return
    try:
        await handler(payload)
    except Exception:
        logger.debug("Tool failure handler raised, ignoring", exc_info=True)
