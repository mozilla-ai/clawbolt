"""Premium override of the OSS ``chat_web_attachments_enabled`` flag.

Premium ships behind CloudFront, which currently 413s phone-sized photo
uploads from the web chat page. Until the upload path is fixed, the
premium app (clawbolt_premium/app.py) flips
``settings.chat_web_attachments_enabled`` to False at import time so the
React app hides the paperclip button.
"""

from fastapi.testclient import TestClient


def test_chat_attachments_flag_off_after_premium_import() -> None:
    """Importing backend.app.main forces the OSS setting to False.

    The override sits at module-level in ``clawbolt_premium/app.py``,
    gated on the absence of ``CHAT_WEB_ATTACHMENTS_ENABLED`` in the env.
    Import the premium app inside the test (instead of relying on a
    prior import) so the assertion is independent of test ordering.
    """
    import backend.app.main  # noqa: F401  needed for module-level side effect
    from backend.app.config import settings

    assert settings.chat_web_attachments_enabled is False


def test_app_config_endpoint_reports_disabled(client: TestClient) -> None:
    """The OSS /api/app/config endpoint surfaces the premium override.

    Frontend uses this endpoint to gate the paperclip affordance, so the
    contract is "when premium is the deployment, the flag comes back False."
    """
    response = client.get("/api/app/config")
    assert response.status_code == 200
    assert response.json() == {"chat_web_attachments_enabled": False}
