"""Regression tests for SPA fallback path traversal protection.

Fixes https://github.com/mozilla-ai/clawbolt/issues/552
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def spa_dist(tmp_path: Path) -> Path:
    """Create a fake frontend/dist directory with an index.html and a nested file."""
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>")
    (dist / "favicon.ico").write_text("icon")
    sub = dist / "assets"
    sub.mkdir()
    (sub / "app.js").write_text("console.log('app')")
    return dist


@pytest.fixture()
def spa_client(spa_dist: Path) -> TestClient:
    """TestClient that serves from the fake dist directory.

    We build a standalone FastAPI app so the conditional
    ``if _FRONTEND_DIST.is_dir()`` block registers the catch-all route
    against our temporary directory.
    """
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI()
    _dist = spa_dist

    _BLOCKED_SUFFIXES = (".env", ".pem", ".key", ".pgpass", ".netrc")
    _BLOCKED_SEGMENTS = {"credentials", "secrets"}

    if _dist.is_dir():
        app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def _spa_fallback(request: Request, full_path: str) -> FileResponse:
            """Serve the SPA index.html for all non-API routes."""
            lower = full_path.lower()
            segments = lower.split("/")
            basename = segments[-1] if segments else ""
            if basename.endswith(_BLOCKED_SUFFIXES) or basename.startswith(".env"):
                raise HTTPException(status_code=404)
            if _BLOCKED_SEGMENTS.intersection(segments):
                raise HTTPException(status_code=404)

            file_path = _dist / full_path
            resolved = file_path.resolve()
            if resolved.is_file() and resolved.is_relative_to(_dist.resolve()):
                return FileResponse(resolved)
            return FileResponse(_dist / "index.html")

    return TestClient(app)


class TestSpaFallbackSecurity:
    """Path traversal protection on the SPA catch-all route."""

    def test_serves_existing_file(self, spa_client: TestClient, spa_dist: Path) -> None:
        """A legitimate file inside dist is served directly."""
        resp = spa_client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.text == "icon"

    def test_unknown_path_returns_index(self, spa_client: TestClient) -> None:
        """Unknown paths fall back to index.html for client-side routing."""
        resp = spa_client.get("/some/unknown/route")
        assert resp.status_code == 200
        assert "SPA" in resp.text

    def test_dotdot_traversal_blocked(self, spa_client: TestClient, spa_dist: Path) -> None:
        """A literal '..' traversal must not escape the dist directory."""
        # Place a secret file outside dist
        secret = spa_dist.parent / "secret.txt"
        secret.write_text("TOP SECRET")

        resp = spa_client.get("/../secret.txt")
        assert resp.status_code == 200
        assert "TOP SECRET" not in resp.text
        assert "SPA" in resp.text

    def test_encoded_dotdot_traversal_blocked(self, spa_client: TestClient, spa_dist: Path) -> None:
        """URL-encoded '..' (%2e%2e) must not bypass the traversal check."""
        secret = spa_dist.parent / "secret.txt"
        secret.write_text("TOP SECRET")

        # %2e is the URL encoding of '.'
        resp = spa_client.get("/%2e%2e/secret.txt")
        assert resp.status_code == 200
        assert "TOP SECRET" not in resp.text
        assert "SPA" in resp.text

    def test_symlink_traversal_blocked(self, spa_client: TestClient, spa_dist: Path) -> None:
        """A symlink inside dist pointing outside must not be served."""
        secret = spa_dist.parent / "secret.txt"
        secret.write_text("TOP SECRET")

        link = spa_dist / "sneaky_link"
        link.symlink_to(secret)

        resp = spa_client.get("/sneaky_link")
        assert resp.status_code == 200
        assert "TOP SECRET" not in resp.text
        assert "SPA" in resp.text

    def test_double_encoded_traversal_blocked(self, spa_client: TestClient, spa_dist: Path) -> None:
        """Double-encoded dots must not bypass the check."""
        secret = spa_dist.parent / "secret.txt"
        secret.write_text("TOP SECRET")

        # %252e decodes to %2e at the first layer
        resp = spa_client.get("/%252e%252e/secret.txt")
        assert resp.status_code == 200
        assert "TOP SECRET" not in resp.text
        assert "SPA" in resp.text


class TestSpaBlockedPaths:
    """Sensitive file patterns return 404 instead of the SPA index.html."""

    @pytest.mark.parametrize(
        "path",
        [
            "/.env",
            "/secrets/.env",
            "/.env.production",
            "/docker/.env",
            "/kubernetes/secrets.env",
            "/.github/workflows/secrets.env",
            "/config/.env.production",
            "/production/.env",
        ],
    )
    def test_env_files_return_404(self, spa_client: TestClient, path: str) -> None:
        """Paths ending with .env or starting with .env must return 404."""
        resp = spa_client.get(path)
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/server.pem",
            "/tls/cert.key",
            "/db/.pgpass",
            "/home/.netrc",
        ],
    )
    def test_key_and_credential_files_return_404(self, spa_client: TestClient, path: str) -> None:
        """Paths with sensitive file extensions must return 404."""
        resp = spa_client.get(path)
        assert resp.status_code == 404

    def test_credentials_segment_returns_404(self, spa_client: TestClient) -> None:
        """Paths containing a 'credentials' segment must return 404."""
        resp = spa_client.get("/config/credentials/db.json")
        assert resp.status_code == 404

    def test_secrets_segment_returns_404(self, spa_client: TestClient) -> None:
        """Paths containing a 'secrets' segment must return 404."""
        resp = spa_client.get("/k8s/secrets/app.yaml")
        assert resp.status_code == 404

    def test_normal_paths_still_serve_spa(self, spa_client: TestClient) -> None:
        """Non-sensitive unknown paths still get the SPA index.html."""
        resp = spa_client.get("/app/dashboard")
        assert resp.status_code == 200
        assert "SPA" in resp.text

    def test_legitimate_files_still_served(self, spa_client: TestClient, spa_dist: Path) -> None:
        """Real files in dist are still served normally."""
        resp = spa_client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.text == "icon"
