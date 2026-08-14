"""Tests for CLI entry point (__main__.py)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.cli import main


class TestCLI:
    def test_no_command_exits(self) -> None:
        """Should print help and exit when no command given."""
        with (
            patch("sys.argv", ["prog"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_serve_command(self) -> None:
        """Should call uvicorn.run with correct args, including default workers."""
        mock_uvicorn = MagicMock()
        with (
            patch("sys.argv", ["prog", "serve", "--port", "9000"]),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
            patch.dict("os.environ", {}, clear=False),
        ):
            main()
            mock_uvicorn.run.assert_called_once_with(
                "backend.app.app:app",
                host="0.0.0.0",
                port=9000,
                reload=False,
                workers=2,
            )

    def test_serve_command_workers_from_env(self) -> None:
        """WEB_CONCURRENCY env var sets the default worker count.

        Multiple workers are needed because we run sync SQLAlchemy inside
        async handlers; one blocked event loop should not take the whole
        site down.
        """
        mock_uvicorn = MagicMock()
        with (
            patch("sys.argv", ["prog", "serve"]),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
            patch.dict("os.environ", {"WEB_CONCURRENCY": "4"}),
        ):
            main()
            kwargs = mock_uvicorn.run.call_args.kwargs
            assert kwargs["workers"] == 4

    def test_serve_command_workers_explicit_flag(self) -> None:
        """``--workers`` flag overrides the env-var default."""
        mock_uvicorn = MagicMock()
        with (
            patch("sys.argv", ["prog", "serve", "--workers", "3"]),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
            patch.dict("os.environ", {"WEB_CONCURRENCY": "8"}),
        ):
            main()
            kwargs = mock_uvicorn.run.call_args.kwargs
            assert kwargs["workers"] == 3

    def test_serve_command_reload_forces_single_worker(self) -> None:
        """``--reload`` mode forces workers=1 (uvicorn requires it)."""
        mock_uvicorn = MagicMock()
        with (
            patch("sys.argv", ["prog", "serve", "--reload", "--workers", "4"]),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            main()
            kwargs = mock_uvicorn.run.call_args.kwargs
            assert kwargs["workers"] == 1
            assert kwargs["reload"] is True

    def test_cleanup_command(self) -> None:
        """Should call cleanup functions."""
        mock_db = MagicMock()
        mock_warn = AsyncMock(return_value=1)
        mock_cleanup = AsyncMock(return_value=2)

        @asynccontextmanager
        async def _mock_db_session_async() -> AsyncGenerator[MagicMock]:
            yield mock_db

        with (
            patch("sys.argv", ["prog", "cleanup"]),
            patch("backend.app.database.db_session_async", _mock_db_session_async),
            patch(
                "backend.app.services.inactive_cleanup.warn_inactive_users",
                mock_warn,
            ),
            patch(
                "backend.app.services.inactive_cleanup.cleanup_inactive_accounts",
                mock_cleanup,
            ),
        ):
            main()
            mock_warn.assert_called_once_with(mock_db)
            mock_cleanup.assert_called_once_with(mock_db)

    def test_cleanup_warn_only(self) -> None:
        """Should only warn when --warn-only is given."""
        mock_db = MagicMock()
        mock_warn = AsyncMock(return_value=3)
        mock_cleanup = AsyncMock()

        @asynccontextmanager
        async def _mock_db_session_async() -> AsyncGenerator[MagicMock]:
            yield mock_db

        with (
            patch("sys.argv", ["prog", "cleanup", "--warn-only"]),
            patch("backend.app.database.db_session_async", _mock_db_session_async),
            patch(
                "backend.app.services.inactive_cleanup.warn_inactive_users",
                mock_warn,
            ),
            patch(
                "backend.app.services.inactive_cleanup.cleanup_inactive_accounts",
                mock_cleanup,
            ),
        ):
            main()
            mock_warn.assert_called_once_with(mock_db)
            mock_cleanup.assert_not_called()
