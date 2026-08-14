"""Observability setup: structured logging with request correlation IDs.

A correlation ID is attached to every request and propagated through:

- Log lines: every record gets a ``request_id`` field (text format shows it
  in brackets; JSON format adds a top-level key).
- Response header: ``X-Request-ID`` is echoed back so the client can quote
  it in support tickets.
- Inbound: if the caller already set ``X-Request-ID``, we reuse it instead
  of minting a new one. Lets a CDN or upstream proxy own the ID.

The ID is stored in a ``ContextVar`` so it crosses any code that runs in the
same asyncio task or context-copied background task. Use ``get_request_id()``
from anywhere to read it.
"""

import contextvars
import json
import logging
import sys
import uuid

from backend.app.config import settings

_NO_REQUEST_ID = "-"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=_NO_REQUEST_ID
)


def new_request_id() -> str:
    """Return a fresh short ID suitable for log correlation."""
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    """Return the current request's correlation ID, or ``-`` if none."""
    return request_id_var.get()


class _RequestIdFilter(logging.Filter):
    """Inject the current request ID onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", _NO_REQUEST_ID),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


_TEXT_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] [%(name)s] %(message)s"


def setup_logging() -> None:
    """Configure logging format based on LOG_FORMAT setting.

    Applies the configured LOG_LEVEL to the ``backend`` logger tree.
    Third-party libraries stay at WARNING to avoid noise.
    """
    # Root stays at WARNING so third-party libraries (httpx, httpcore,
    # python-telegram-bot, etc.) do not surface INFO-level lines. httpx in
    # particular logs full request URLs at INFO, which would leak query-
    # string credentials such as the BlueBubbles password.
    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        root.addHandler(handler)

    # Apply the configured log level to the app logger tree so that
    # LOG_LEVEL=DEBUG works without raising third-party libraries with it.
    app_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.getLogger("backend").setLevel(app_level)

    # Belt-and-suspenders: pin known-noisy loggers to WARNING in case a
    # downstream caller raises the root level later in the process.
    for noisy in ("httpx", "httpcore", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Attach the request-id filter to every existing handler. Filters run on
    # the handler so they apply to records propagated up from any logger.
    rid_filter = _RequestIdFilter()
    if settings.log_format == "json":
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FORMAT)

    for handler in root.handlers:
        handler.setFormatter(formatter)
        # Avoid stacking duplicate filters on repeated setup_logging() calls.
        if not any(isinstance(f, _RequestIdFilter) for f in handler.filters):
            handler.addFilter(rid_filter)
