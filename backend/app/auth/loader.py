import importlib

from backend.app.auth.base import AuthBackend
from backend.app.config import settings

_backend: AuthBackend | None = None
_loaded: bool = False


def get_auth_backend() -> AuthBackend | None:
    global _backend, _loaded
    if _loaded:
        return _backend
    if settings.premium_plugin:
        module = importlib.import_module(settings.premium_plugin)
        _backend = module.get_auth_backend()
    _loaded = True
    return _backend
