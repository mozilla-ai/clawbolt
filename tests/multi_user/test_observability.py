"""Tests for observability: structured logging, request logging middleware."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.app.middleware.request_logging import RequestLoggingMiddleware
from backend.app.observability import (
    _JsonFormatter,
    _RequestIdFilter,
    get_request_id,
    new_request_id,
    request_id_var,
    setup_logging,
)


class TestJsonFormatter:
    def test_formats_as_valid_json(self) -> None:
        """JSON formatter should produce valid JSON output."""
        formatter = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed

    def test_includes_exception(self) -> None:
        """JSON formatter should include exception info when present."""
        formatter = _JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="something failed",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]


class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def _save_formatters(self) -> None:  # type: ignore[misc]
        """Save and restore root logger handler formatters between tests."""
        root = logging.getLogger()
        original = [(h, h.formatter) for h in root.handlers]
        yield
        for handler, fmt in original:
            handler.setFormatter(fmt)

    def test_json_format_sets_json_formatter(self) -> None:
        """When LOG_FORMAT=json, root handlers should use JSON formatter."""
        with patch("backend.app.observability.settings") as mock_settings:
            mock_settings.log_format = "json"
            setup_logging()
            root = logging.getLogger()
            assert any(isinstance(h.formatter, _JsonFormatter) for h in root.handlers)

    def test_text_format_does_not_set_json_formatter(self) -> None:
        """When LOG_FORMAT=text, root handlers should not use JSON formatter."""
        with patch("backend.app.observability.settings") as mock_settings:
            mock_settings.log_format = "text"
            setup_logging()
            root = logging.getLogger()
            assert not any(isinstance(h.formatter, _JsonFormatter) for h in root.handlers)

    def test_debug_level_applied(self) -> None:
        """When LOG_LEVEL=DEBUG, clawbolt_premium logger should be set to DEBUG."""
        with patch("backend.app.config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"
            with patch("backend.app.observability.settings") as mock_ps:
                mock_ps.log_format = "text"
                setup_logging()

        assert logging.getLogger("clawbolt_premium").level == logging.DEBUG

    def test_info_level_default(self) -> None:
        """When LOG_LEVEL=INFO, clawbolt_premium logger should be set to INFO."""
        with patch("backend.app.config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            with patch("backend.app.observability.settings") as mock_ps:
                mock_ps.log_format = "text"
                setup_logging()

        assert logging.getLogger("clawbolt_premium").level == logging.INFO

    def test_third_party_loggers_stay_at_warning(self) -> None:
        """httpx, httpcore, and telegram must not log at INFO.

        httpx logs full request URLs at INFO, which would leak the
        BlueBubbles ``password`` query parameter and recipient phone
        numbers into production logs (issue #1082). Pinning these
        loggers to WARNING is part of the fix; the other half is the
        explicit root-level setting.
        """
        with patch("backend.app.config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"  # even with DEBUG app level
            with patch("backend.app.observability.settings") as mock_ps:
                mock_ps.log_format = "text"
                setup_logging()

        assert logging.getLogger().level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("telegram").level == logging.WARNING

    def test_httpx_info_records_are_filtered(self, caplog: pytest.LogCaptureFixture) -> None:
        """An INFO-level emit on the httpx logger must not pass through.

        Regression test for #1082: a real httpx INFO record with a URL
        containing ``password=`` should be suppressed once setup_logging
        has run.
        """
        with patch("backend.app.config.settings") as mock_settings:
            mock_settings.log_level = "INFO"
            with patch("backend.app.observability.settings") as mock_ps:
                mock_ps.log_format = "text"
                setup_logging()

        with caplog.at_level(logging.WARNING, logger="httpx"):
            logging.getLogger("httpx").info(
                "HTTP Request: POST https://example/api/v1/chat/typing?password=secret"
            )
        assert not any("password=secret" in r.getMessage() for r in caplog.records)


class TestRequestLoggingMiddleware:
    @pytest.fixture()
    def test_app(self) -> TestClient:
        """Create a minimal Starlette app with request logging."""

        async def homepage(request: object) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/", homepage)])
        app.add_middleware(RequestLoggingMiddleware)  # ty: ignore[invalid-argument-type]
        return TestClient(app)

    def test_logs_request(self, test_app: TestClient, caplog: pytest.LogCaptureFixture) -> None:
        """Should log method, path, status code, and duration."""
        with caplog.at_level(logging.INFO):
            resp = test_app.get("/")
        assert resp.status_code == 200
        log_line = next((msg for msg in caplog.messages if "GET" in msg and "/" in msg), None)
        assert log_line is not None
        assert "200" in log_line

    @pytest.mark.parametrize(
        "skip_path",
        [
            "/api/admin/version",  # admin overview 60s deploy-detect poll
            "/api/health",  # Dockerfile HEALTHCHECK 30s
            "/api/health/live",  # platform liveness probe
        ],
    )
    def test_skips_high_frequency_poll_paths(
        self, caplog: pytest.LogCaptureFixture, skip_path: str
    ) -> None:
        """Fixed-timer infrastructure pollers produce zero diagnostic value
        at INFO, so the access line is suppressed for each path in
        `_SKIP_LOG_PATHS`. The correlation-ID header must still be set so
        traces stitched by request ID keep working. Regression for the
        dev.clawbolt.ai log noise reported in chat."""

        async def endpoint(request: object) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route(skip_path, endpoint)])
        app.add_middleware(RequestLoggingMiddleware)  # ty: ignore[invalid-argument-type]

        with caplog.at_level(logging.INFO), TestClient(app) as client:
            resp = client.get(skip_path)
        assert resp.status_code == 200
        access_records = [
            r for r in caplog.records if r.name == "backend.app.middleware.request_logging"
        ]
        assert access_records == []
        assert resp.headers.get("x-request-id")

    def test_middleware_is_pure_asgi(self) -> None:
        """RequestLoggingMiddleware should be a pure ASGI middleware."""
        mock_app = MagicMock()
        middleware = RequestLoggingMiddleware(mock_app)
        assert callable(middleware)
        assert not hasattr(middleware, "dispatch")

    def test_echoes_request_id_header(self, test_app: TestClient) -> None:
        """Response should always carry an X-Request-ID header."""
        resp = test_app.get("/")
        rid = resp.headers.get("x-request-id", "")
        assert rid
        assert rid != "-"

    def test_reuses_inbound_request_id(self, test_app: TestClient) -> None:
        """If the caller sets X-Request-ID, the middleware should preserve it."""
        resp = test_app.get("/", headers={"X-Request-ID": "client-trace-abc"})
        assert resp.headers["x-request-id"] == "client-trace-abc"

    def test_logs_carry_request_id(
        self, test_app: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Records emitted during a request should have the request_id attribute."""
        with caplog.at_level(logging.INFO):
            test_app.get("/", headers={"X-Request-ID": "trace-xyz"})
        access = next(
            (r for r in caplog.records if r.name == "backend.app.middleware.request_logging"),
            None,
        )
        assert access is not None
        assert getattr(access, "request_id", None) == "trace-xyz"

    def test_resets_context_after_request(self, test_app: TestClient) -> None:
        """After the response returns, the context var should fall back to default."""
        test_app.get("/", headers={"X-Request-ID": "scoped-id"})
        # Outside the request, no ID is set
        assert get_request_id() == "-"

    def test_rejects_malicious_request_id_with_crlf(self, test_app: TestClient) -> None:
        """A CRLF-injected X-Request-ID must be discarded, not echoed."""
        # The HTTP client may strip CRLF from headers before sending; force it.
        resp = test_app.get(
            "/",
            headers={"X-Request-ID": "abc\r\nSet-Cookie: x=1"},
        )
        echoed = resp.headers["x-request-id"]
        assert "\r" not in echoed
        assert "\n" not in echoed
        assert "Set-Cookie" not in echoed
        # Must have generated a fresh ID instead of using the malicious one.
        assert echoed != "abc"

    def test_rejects_overly_long_request_id(self, test_app: TestClient) -> None:
        """An absurdly long X-Request-ID must be discarded."""
        resp = test_app.get("/", headers={"X-Request-ID": "x" * 1000})
        assert len(resp.headers["x-request-id"]) <= 128

    def test_rejects_request_id_with_special_chars(self, test_app: TestClient) -> None:
        """Spaces, semicolons, etc. must be discarded."""
        resp = test_app.get("/", headers={"X-Request-ID": "abc; injected"})
        assert resp.headers["x-request-id"] != "abc; injected"

    def test_no_duplicate_request_id_header_when_downstream_sets_one(self) -> None:
        """If downstream middleware sets X-Request-ID, the response must still
        carry exactly one (the middleware's), not both."""
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        async def downstream(request: object) -> Response:
            # Simulate a downstream handler that ALSO sets X-Request-ID.
            return Response("ok", headers={"X-Request-ID": "downstream-set"})

        app = Starlette(routes=[Route("/", downstream)])
        app.add_middleware(RequestLoggingMiddleware)  # ty: ignore[invalid-argument-type]

        with TestClient(app) as client:
            resp = client.get("/", headers={"X-Request-ID": "client-trace"})

        rid_headers = [v for k, v in resp.headers.items() if k.lower() == "x-request-id"]
        assert len(rid_headers) == 1
        assert rid_headers[0] == "client-trace"


class TestRequestIdHelpers:
    def test_new_request_id_is_short_and_unique(self) -> None:
        a = new_request_id()
        b = new_request_id()
        assert len(a) == 12
        assert a != b

    def test_filter_attaches_request_id(self) -> None:
        token = request_id_var.set("filter-test")
        try:
            record = logging.LogRecord(
                name="t",
                level=logging.INFO,
                pathname="t.py",
                lineno=1,
                msg="m",
                args=(),
                exc_info=None,
            )
            assert _RequestIdFilter().filter(record) is True
            assert getattr(record, "request_id", None) == "filter-test"
        finally:
            request_id_var.reset(token)

    def test_json_formatter_includes_request_id(self) -> None:
        token = request_id_var.set("json-test")
        try:
            record = logging.LogRecord(
                name="t",
                level=logging.INFO,
                pathname="t.py",
                lineno=1,
                msg="hi",
                args=(),
                exc_info=None,
            )
            _RequestIdFilter().filter(record)
            output = _JsonFormatter().format(record)
            assert json.loads(output)["request_id"] == "json-test"
        finally:
            request_id_var.reset(token)
