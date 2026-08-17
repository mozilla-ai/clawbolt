from backend.app.auth.base import AuthBackend
from backend.app.auth.oauth_backend import OAuthBackend
from backend.app.config import settings
from backend.app.security.encryption import KEKProvider, LocalKEKProvider

_backend: AuthBackend | None = None
_loaded: bool = False

_kek_provider: KEKProvider | None = None


def get_auth_backend() -> AuthBackend | None:
    """Return the auth backend the frontend should render a login for.

    ``multi_user`` mode uses the built-in Google OAuth backend, and
    ``single_user`` has no backend at all, which is how
    ``/api/auth/config`` tells the SPA that no login is required.
    """
    global _backend, _loaded
    if _loaded:
        return _backend
    if settings.auth_mode == "multi_user":
        _backend = OAuthBackend()
    _loaded = True
    return _backend


def get_kek_provider() -> KEKProvider:
    """Return the active KEK provider.

    Resolution order, first match wins:

    1. KMS envelope encryption, when ``KMS_KEY_ARN`` is set.
    2. ``LocalKEKProvider``, wrapping with a key derived from
       ``ENCRYPTION_KEY``.

    This function is load-bearing for data, not just for startup:
    returning a different provider than the one that wrote a row makes
    every ``EncryptedString`` column on it unreadable. Changing the order
    or the conditions is a data migration, not a refactor.
    """
    global _kek_provider
    if _kek_provider is not None:
        return _kek_provider
    if settings.kms_key_arn:
        # Imported here rather than at module scope so deployments without
        # KMS do not pay the boto3 import cost.
        from backend.app.security.kms import KMSEnvelopeKEKProvider

        _kek_provider = KMSEnvelopeKEKProvider(
            kms_key_arn=settings.kms_key_arn,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
        )
        return _kek_provider
    _kek_provider = LocalKEKProvider()
    return _kek_provider


def reset_kek_provider() -> None:
    """Reset the cached KEK provider. Test-only."""
    global _kek_provider
    _kek_provider = None


def reset_auth_backend() -> None:
    """Reset the cached auth backend. Test-only."""
    global _backend, _loaded
    _backend = None
    _loaded = False
