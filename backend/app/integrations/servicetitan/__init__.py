"""ServiceTitan integration package.

The integration is split across:

* ``_fake.py``: an in-process fake of the ServiceTitan REST API, used until
  real sandbox credentials are provisioned and as a deterministic backend
  for tests. Exposed as an ``httpx.MockTransport`` so callers can swap it
  in for a real network transport without any code changes.

The auth, factory, service, and tool modules are added by the
follow-on issues in the ServiceTitan mock-backed MVP milestone.
"""

from backend.app.integrations.servicetitan._fake import (
    ServiceTitanFakeBackend,
    build_fake_transport,
    get_default_fake_backend,
)

__all__ = [
    "ServiceTitanFakeBackend",
    "build_fake_transport",
    "get_default_fake_backend",
]
