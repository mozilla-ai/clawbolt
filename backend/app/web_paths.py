"""Shared web path constants for URL builders.

The OSS React router (``../clawbolt/frontend/src/App.tsx``) is the canonical
source of truth for SPA routes. Any backend code that constructs a URL the
user will click on (transactional emails, admin redirects, webhook return
links) should import the matching constant from here rather than hard-coding
the path string. The mirror constant on the TS side lives in
``frontend/src/extensions/routes.tsx`` so the same name resolves on both
languages.

Keeping these names short, single-purpose, and free of ``APP_BASE_URL`` lets
callers compose the absolute URL however they need (``rstrip('/')``, etc.).
"""

from __future__ import annotations

# SPA login page. Defined by OSS ``App.tsx`` as ``<Route path="/app/login">``.
LOGIN_PATH = "/app/login"
