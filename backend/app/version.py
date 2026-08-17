"""Build-time version metadata surfaced to the admin UI.

The admin overview polls ``/api/admin/version`` so a tab kept open across a
deploy can detect the new release and reload itself. ``started_at`` is the
load-bearing field for that comparison: every fresh process picks up a new
import-time timestamp, so we do not depend on commit env vars being stamped
to detect a deploy. The commit / version fields are display-only.

The ``premium_*`` fields describe the deploy wrapper: mozilla.ai's hosted
deployment builds this repo inside ``clawbolt-premium``, which stamps the
commit it ships into ``OSS_REF`` / ``OSS_VERSION`` and its own release into
``PREMIUM_VERSION``. A plain self-host has none of them, so both fields
report their unknown values and the admin UI renders the OSS side only.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

logger = logging.getLogger(__name__)

# Frozen at import. New process => new value, which is what the admin client
# polls against to detect a deploy.
_STARTED_AT: _dt.datetime = _dt.datetime.now(_dt.UTC)

# Written by the deploy wrapper: /app/OSS_REF in the runtime image (see its
# Dockerfile), and the wrapper's repo root when it is run from there in dev.
# Resolve once at import; a missing file is non-fatal and is the normal case
# for a plain self-host.
_OSS_REF_CANDIDATES = (
    Path("/app/OSS_REF"),
    Path.cwd() / "OSS_REF",
)

# OSS_VERSION sits next to OSS_REF and stores the tag (e.g. ``v0.4.4``).
# Empty when OSS_REF was bumped to an untagged main commit; the UI renders
# that case as hash-only.
_OSS_VERSION_CANDIDATES = (
    Path("/app/OSS_VERSION"),
    Path.cwd() / "OSS_VERSION",
)

# The wrapper's own release version, stamped next to the two files above.
# It used to be read from the installed ``clawbolt-premium`` distribution,
# but the wrapper stopped shipping a Python package once the last of its
# code moved into this repo, so the number travels as a file like the rest
# of the build metadata.
_PREMIUM_VERSION_CANDIDATES = (
    Path("/app/PREMIUM_VERSION"),
    Path.cwd() / "PREMIUM_VERSION",
)


def _read_first_nonempty(candidates: tuple[Path, ...], default: str) -> str:
    for candidate in candidates:
        try:
            content = candidate.read_text().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if content:
            return content
    return default


@lru_cache(maxsize=1)
def _premium_version() -> str:
    from_file = _read_first_nonempty(_PREMIUM_VERSION_CANDIDATES, default="")
    if from_file:
        return from_file
    # Wrapper images built before the file existed still install the
    # package. Drop this branch once no such image can be deployed.
    try:
        return _pkg_version("clawbolt-premium")
    except PackageNotFoundError:
        return "0.0.0"


@lru_cache(maxsize=1)
def _premium_commit() -> str:
    # Railway auto-injects RAILWAY_GIT_COMMIT_SHA at runtime for every deploy,
    # so we get this for free without touching the Dockerfile. The explicit
    # CLAWBOLT_PREMIUM_COMMIT escape hatch covers non-Railway hosts.
    for key in ("CLAWBOLT_PREMIUM_COMMIT", "RAILWAY_GIT_COMMIT_SHA"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return "unknown"


@lru_cache(maxsize=1)
def _oss_commit() -> str:
    override = os.environ.get("CLAWBOLT_OSS_COMMIT")
    if override:
        return override.strip()
    return _read_first_nonempty(_OSS_REF_CANDIDATES, default="unknown")


@lru_cache(maxsize=1)
def _oss_version() -> str:
    # Empty rather than "unknown" because untagged OSS bumps are a normal
    # operating state and the UI hides the version slot when empty.
    override = os.environ.get("CLAWBOLT_OSS_VERSION")
    if override is not None:
        return override.strip()
    return _read_first_nonempty(_OSS_VERSION_CANDIDATES, default="")


def get_version_info() -> dict[str, str]:
    """Return the build metadata payload for ``GET /api/admin/version``."""
    return {
        "premium_version": _premium_version(),
        "premium_commit": _premium_commit(),
        "oss_version": _oss_version(),
        "oss_commit": _oss_commit(),
        "started_at": _STARTED_AT.isoformat(),
    }


# Surface the resolved version in deploy logs so postmortems do not need to
# hit /api/admin/version. Logged once at module import (i.e. at app boot).
logger.info(
    "Loaded version metadata: premium=%s commit=%s oss=%s oss_commit=%s started_at=%s",
    _premium_version(),
    _premium_commit(),
    _oss_version() or "(untagged)",
    _oss_commit(),
    _STARTED_AT.isoformat(),
)
