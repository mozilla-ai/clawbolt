"""FastAPI auto-generated API doc routes must be stripped from the premium app.

Premium does not expose a public API, and /docs is claimed by the React SPA
(user guide). Without stripping these routes, Swagger UI wins route matching
on /docs and shadows the SPA route.
"""

from fastapi.testclient import TestClient


class TestFastApiDocsRemoved:
    def test_auto_doc_routes_removed_from_app(self) -> None:
        from tests.multi_user.conftest import MULTI_USER_APP as app

        removed = {"openapi", "swagger_ui_html", "swagger_ui_redirect", "redoc_html"}
        found = {getattr(r, "name", None) for r in app.routes} & removed
        assert not found, f"FastAPI auto-doc routes still registered: {found}"

    def test_openapi_schema_not_served(self, client: TestClient) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code != 200 or "application/json" not in resp.headers.get(
            "content-type", ""
        )

    def test_docs_does_not_serve_swagger_ui(self, client: TestClient) -> None:
        resp = client.get("/docs")
        assert "swagger-ui" not in resp.text.lower()

    def test_redoc_not_served(self, client: TestClient) -> None:
        resp = client.get("/redoc")
        assert "redoc" not in resp.text.lower() or resp.status_code != 200
