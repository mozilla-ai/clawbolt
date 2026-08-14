import importlib
from types import ModuleType

from backend.app.auth.base import AuthBackend
from backend.app.auth.oauth_backend import OAuthBackend
from backend.app.config import settings
from backend.app.security.encryption import KEKProvider, LocalKEKProvider

_backend: AuthBackend | None = None
_loaded: bool = False

_kek_provider: KEKProvider | None = None


def load_plugin_module() -> ModuleType | None:
    """Import the configured ``PREMIUM_PLUGIN`` module, or return ``None``.

    The single place OSS reaches for the plugin. Callers that only need
    the module's import side effects (a plugin registering itself through
    a module-level setter) use this directly rather than going through a
    hook that would also require the plugin to expose that hook.
    """
    if not settings.premium_plugin:
        return None
    return importlib.import_module(settings.premium_plugin)


def get_auth_backend() -> AuthBackend | None:
    """Return the auth backend the frontend should render a login for.

    A plugin wins if one is configured. Otherwise ``multi_user`` mode uses
    the built-in Google OAuth backend, and ``single_user`` has no backend
    at all, which is how ``/api/auth/config`` tells the SPA that no login
    is required.
    """
    global _backend, _loaded
    if _loaded:
        return _backend
    module = load_plugin_module()
    if module is not None:
        _backend = module.get_auth_backend()
    elif settings.auth_mode == "multi_user":
        _backend = OAuthBackend()
    _loaded = True
    return _backend


def get_kek_provider() -> KEKProvider:
    """Return the active KEK provider.

    Resolution order, first match wins:

    1. A plugin's ``get_kek_provider()``, when one is configured and
       returns something. A plugin may return ``None`` to decline at
       runtime, in which case resolution continues here.
    2. KMS envelope encryption, when ``KMS_KEY_ARN`` is set.
    3. ``LocalKEKProvider``, wrapping with a key derived from
       ``ENCRYPTION_KEY``.

    This function is load-bearing for data, not just for startup:
    returning a different provider than the one that wrote a row makes
    every ``EncryptedString`` column on it unreadable. Changing the order
    or the conditions is a data migration, not a refactor.
    """
    global _kek_provider
    if _kek_provider is not None:
        return _kek_provider
    module = load_plugin_module()
    if module is not None and hasattr(module, "get_kek_provider"):
        plugin_provider = module.get_kek_provider()
        if plugin_provider is not None:
            _kek_provider = plugin_provider
            return _kek_provider
    if settings.kms_key_arn:
        # Imported here rather than at module scope so deployments without
        # KMS do not pay the boto3 import cost, which is the same reason
        # the premium plugin deferred it before the move.
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
