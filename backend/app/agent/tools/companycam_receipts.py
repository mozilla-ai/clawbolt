"""Shared helpers for building human-readable CompanyCam tool receipts.

CompanyCam entity URLs are deterministic (``app.companycam.com/{kind}/{id}``)
so receipts can link to the web app without any extra HTTP round trip.
Every helper here is pure: no I/O, no DB, no network.

The tools pass LLM-authored or API-authored text through these helpers so
the receipt footer in iMessage, SMS, and Telegram stays compact and can
never be used to forge fake receipt lines by injecting newlines.
"""

from __future__ import annotations

import re

from backend.app.config import settings
from backend.app.services.companycam_models import Photo, Project

# CompanyCam entity ids are numeric strings. Gate URL construction on this
# so a garbled id from the API or a confused LLM cannot poison the URL
# (e.g. "94772883?foo=bar" or "../admin" never reach the output).
_ID_RE = re.compile(r"^\d+$")

# Collapse newlines, tabs, and other control chars so LLM/user-authored
# text cannot break out of the "- {action} {target}\n  {url}" shape.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(text: str, max_chars: int) -> str:
    """Scrub control chars, collapse whitespace, cap length.

    Used for any string that started as LLM output, user comment, or
    CompanyCam API text that may contain stray newlines. Truncation adds
    a trailing ellipsis when the original did not fit.
    """
    if not text:
        return ""
    flat = _CTRL_RE.sub(" ", text)
    flat = re.sub(r"\s+", " ", flat).strip()
    if not flat:
        return ""
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "\u2026"


def _web_base() -> str:
    """Return the CompanyCam web base URL from settings, without a trailing slash."""
    return settings.companycam_web_base.rstrip("/")


def project_url(project_id: str) -> str | None:
    """Return the web URL for a CompanyCam project, or None when the id
    is missing or not a numeric string.

    Links to the ``/photos`` tab because that is the primary view
    contractors use after tapping a project link in iMessage or SMS.
    """
    if not project_id or not _ID_RE.match(project_id):
        return None
    return f"{_web_base()}/projects/{project_id}/photos"


def photo_url(photo_id: str) -> str | None:
    """Return the web URL for a CompanyCam photo, or None when the id is
    missing or not a numeric string."""
    if not photo_id or not _ID_RE.match(photo_id):
        return None
    return f"{_web_base()}/photos/{photo_id}"


def project_target(project: Project | None) -> str:
    """Human-readable target for a project receipt. Never a raw id."""
    if project and project.name:
        sanitized = _sanitize(project.name, 60)
        if sanitized:
            return sanitized
    return "project"


def photo_target(photo: Photo | None) -> str:
    """Human-readable target for a photo receipt. Never a raw id.

    Guards against structured data (dict repr, JSON) that the LLM may
    have passed as the description parameter. A description that starts
    with ``{`` or ``[`` is almost certainly not prose, so we fall back
    to the generic word rather than surfacing raw braces in the footer.
    """
    if photo and photo.description:
        stripped = photo.description.strip()
        if stripped.startswith(("{", "[")):
            return "photo"
        desc = _sanitize(stripped, 60)
        if desc:
            return desc
    return "photo"


def comment_target(content: str) -> str:
    """Human-readable target for an add-comment receipt."""
    return _sanitize(content, 40) or "comment"


def tags_target(tag_names: list[str]) -> str:
    """Human-readable target for a tag-photo receipt.

    Caps each tag at 25 chars, dedupes while preserving insertion order,
    and truncates the list at 3 tags + '+N more' so a tag run cannot
    blow up the footer.
    """
    cleaned = [_sanitize(name, 25) for name in tag_names if name]
    cleaned = [name for name in cleaned if name]
    # Order-preserving dedupe so ["kitchen", "kitchen", "demo"] collapses
    # to ["kitchen", "demo"] without re-sorting.
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned:
        return "photo"
    if len(cleaned) <= 3:
        return ", ".join(cleaned)
    return ", ".join(cleaned[:3]) + f" +{len(cleaned) - 3} more"
