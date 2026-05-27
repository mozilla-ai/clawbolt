from fastapi.testclient import TestClient

from backend.app.config import settings


def test_app_config_default(client: TestClient) -> None:
    """OSS default keeps the chat web upload affordance enabled."""
    response = client.get("/api/app/config")
    assert response.status_code == 200
    assert response.json() == {"chat_web_attachments_enabled": True}


def test_app_config_reflects_settings_override(client: TestClient) -> None:
    """Deployments that flip the flag (e.g. premium under CloudFront) see it."""
    original = settings.chat_web_attachments_enabled
    settings.chat_web_attachments_enabled = False
    try:
        response = client.get("/api/app/config")
        assert response.status_code == 200
        assert response.json() == {"chat_web_attachments_enabled": False}
    finally:
        settings.chat_web_attachments_enabled = original
