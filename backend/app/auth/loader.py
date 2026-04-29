import importlib

from backend.app.auth.base import AuthBackend
from backend.app.config import settings
from backend.app.security.encryption import KEKProvider, LocalKEKProvider

_backend: AuthBackend | None = None
_loaded: bool = False

_kek_provider: KEKProvider | None = None


def get_auth_backend() -> AuthBackend | None:
    global _backend, _loaded
    if _loaded:
        return _backend
    if settings.premium_plugin:
        module = importlib.import_module(settings.premium_plugin)
        _backend = module.get_auth_backend()
    _loaded = True
    return _backend


def get_kek_provider() -> KEKProvider:
    """Return the active KEK provider.

    Premium plugins override the OSS default by exposing
    ``get_kek_provider()`` from their plugin module. The OSS default is
    a process-singleton ``LocalKEKProvider`` derived from
    ``settings.encryption_key``.
    """
    global _kek_provider
    if _kek_provider is not None:
        return _kek_provider
    if settings.premium_plugin:
        module = importlib.import_module(settings.premium_plugin)
        if hasattr(module, "get_kek_provider"):
            _kek_provider = module.get_kek_provider()
            return _kek_provider
    _kek_provider = LocalKEKProvider()
    return _kek_provider


def reset_kek_provider() -> None:
    """Reset the cached KEK provider. Test-only."""
    global _kek_provider
    _kek_provider = None
