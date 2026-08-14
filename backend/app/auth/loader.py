import importlib
from types import ModuleType

from backend.app.auth.base import AuthBackend
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
    global _backend, _loaded
    if _loaded:
        return _backend
    module = load_plugin_module()
    if module is not None:
        _backend = module.get_auth_backend()
    _loaded = True
    return _backend


def get_kek_provider() -> KEKProvider:
    """Return the active KEK provider.

    Premium plugins override the OSS default by exposing
    ``get_kek_provider()`` from their plugin module. A plugin may
    return ``None`` from that hook to opt out of the override at
    runtime (e.g. when KMS isn't configured yet); in that case we fall
    back to the OSS default. This lets premium ship the KMS provider
    code dormant and have it activate the moment the env vars are set,
    without a code change.
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
    _kek_provider = LocalKEKProvider()
    return _kek_provider


def reset_kek_provider() -> None:
    """Reset the cached KEK provider. Test-only."""
    global _kek_provider
    _kek_provider = None
