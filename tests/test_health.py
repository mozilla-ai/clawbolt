from fastapi.testclient import TestClient


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "ok", "database": "ok"}


def test_health_live_endpoint(client: TestClient) -> None:
    """The liveness probe must respond instantly without touching the DB.

    Used as the deployment platform's healthcheck path so a stuck DB or
    a blocked event loop on another worker does not also pile up on the
    healthcheck and prevent traffic from rolling to a fresh container.
    """
    response = client.get("/api/health/live")
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "ok", "database": "not_checked"}
