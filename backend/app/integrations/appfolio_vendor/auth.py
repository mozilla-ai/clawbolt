"""Magic-link authentication and token storage for AppFolio Vendor Portal.

Three concerns live here:

1. **Magic-link parsing.** The user pastes a URL like
   ``https://vendor.appfolio.com/?magic_link_token=eyJ...``; we extract
   the token regardless of which AppFolio host the URL points at.

2. **Fingerprint generation and persistence.** AppFolio binds the
   Bearer JWT issued at ``/access`` to a client fingerprint and
   validates the ``X-Fingerprint`` header on subsequent calls. We
   generate a random hex string at first connect and persist it in
   ``oauth_tokens.extra_json`` so the same value is reused for the
   life of the credential.

3. **Token persistence.** We piggy-back on the existing ``oauth_tokens``
   table: ``access_token`` holds the JWT, ``refresh_token`` holds the
   OAuth2 refresh token (both envelope-encrypted at rest via
   ``EncryptedString``), and ``extra_json`` carries the fingerprint plus
   any AppFolio-specific metadata.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import db_session_async
from backend.app.models import OAuthToken

logger = logging.getLogger(__name__)

INTEGRATION_NAME = "appfolio_vendor"
"""Integration key used in the ``oauth_tokens.integration`` column."""

_FINGERPRINT_BYTES = 16
"""32-char hex string. Mirrors the length of fingerprint2.js x64 hash output."""


@dataclass
class AppFolioCredential:
    """Authenticated AppFolio credential loaded from storage."""

    user_id: str
    jwt: str
    fingerprint: str
    customer_ids: list[str]
    """AppFolio customer (property manager) IDs the vendor works under."""

    extra: dict[str, Any]
    refresh_token: str = ""


class MagicLinkError(ValueError):
    """Raised when a magic-link URL cannot be parsed."""


def extract_magic_link_token(text: str) -> str:
    """Pull the ``magic_link_token`` value out of a URL or raw token string.

    Accepts a full URL (``https://vendor.appfolio.com/?magic_link_token=XXX``),
    a query fragment (``?magic_link_token=XXX``), or the bare token. Whitespace
    is stripped. Raises ``MagicLinkError`` when no token is found.
    """
    candidate = text.strip()
    if not candidate:
        raise MagicLinkError("empty input; paste the full magic-link URL")

    if "magic_link_token=" in candidate:
        if "://" in candidate:
            parsed = urlparse(candidate)
            params = parse_qs(parsed.query)
            tokens = params.get("magic_link_token") or []
            if not tokens:
                raise MagicLinkError("URL has no magic_link_token parameter")
            return tokens[0]
        # Bare query fragment: split on '=' once.
        _, _, value = candidate.partition("magic_link_token=")
        return value.split("&", 1)[0]

    # Heuristic: a JWT has two dots. Treat anything else with no '=' as a
    # bare token.
    if "=" not in candidate:
        return candidate

    raise MagicLinkError("could not find magic_link_token in input")


def generate_fingerprint() -> str:
    """Generate a random per-tenant client fingerprint.

    AppFolio's web client computes this from browser characteristics; we
    replace it with a random hex string of the same length, persisted at
    first connect and reused thereafter.
    """
    return secrets.token_hex(_FINGERPRINT_BYTES)


async def load_credential(user_id: str) -> AppFolioCredential | None:
    """Load the persisted AppFolio credential for a user, if any."""
    async with db_session_async() as session:
        return await _load_credential_in_session(session, user_id)


async def _load_credential_in_session(
    session: AsyncSession, user_id: str
) -> AppFolioCredential | None:
    stmt = sa.select(OAuthToken).where(
        OAuthToken.user_id == user_id,
        OAuthToken.integration == INTEGRATION_NAME,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None or not row.access_token:
        return None
    extra = json.loads(row.extra_json or "{}")
    fingerprint = extra.get("fingerprint", "")
    if not fingerprint:
        # Stale row from a previous failed connect; treat as not connected.
        return None
    # Prefer the dedicated encrypted column; fall back to the legacy
    # ``extra_json`` slot so credentials persisted before the column move
    # keep working until the next refresh rewrites them. The fallback can
    # be dropped once all live tokens have rotated past it.
    refresh_token = row.refresh_token or extra.get("refresh_token", "")
    return AppFolioCredential(
        user_id=user_id,
        jwt=row.access_token,
        fingerprint=fingerprint,
        customer_ids=list(extra.get("customer_ids") or []),
        extra=extra,
        refresh_token=refresh_token,
    )


async def save_credential(
    user_id: str,
    jwt: str,
    fingerprint: str,
    customer_ids: list[str],
    extra_metadata: dict[str, Any] | None = None,
    refresh_token: str = "",
) -> None:
    """Persist (or replace) the AppFolio credential for a user.

    The JWT and refresh token are written to the dedicated encrypted
    columns on ``oauth_tokens``; only the fingerprint, customer IDs, and
    free-form metadata land in ``extra_json``.
    """
    extra: dict[str, Any] = {
        "fingerprint": fingerprint,
        "customer_ids": customer_ids,
    }
    if extra_metadata:
        extra.update(extra_metadata)
    # Strip any legacy ``refresh_token`` left in extra by an older code
    # path so we never end up with both an encrypted and a plaintext copy.
    extra.pop("refresh_token", None)
    now = datetime.now(UTC)
    async with db_session_async() as session:
        stmt = sa.select(OAuthToken).where(
            OAuthToken.user_id == user_id,
            OAuthToken.integration == INTEGRATION_NAME,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = OAuthToken(
                user_id=user_id,
                integration=INTEGRATION_NAME,
                access_token=jwt,
                refresh_token=refresh_token,
                token_type="Bearer",
                extra_json=json.dumps(extra),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.access_token = jwt
            row.refresh_token = refresh_token
            row.token_type = "Bearer"
            row.extra_json = json.dumps(extra)
            row.updated_at = now
        await session.commit()


async def upsert_fingerprint(user_id: str) -> str:
    """Return the user's persisted fingerprint, creating one if absent.

    Called before the first ``/access`` exchange so the same fingerprint
    is sent to AppFolio in the body and in subsequent ``X-Fingerprint``
    headers. Persists to a partial row (no JWT yet) so a crash mid-flow
    does not lose the fingerprint and bind the user to a different one
    on retry.
    """
    async with db_session_async() as session:
        stmt = sa.select(OAuthToken).where(
            OAuthToken.user_id == user_id,
            OAuthToken.integration == INTEGRATION_NAME,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None and row.extra_json:
            extra = json.loads(row.extra_json)
            existing = extra.get("fingerprint")
            if existing:
                return existing
        fingerprint = generate_fingerprint()
        now = datetime.now(UTC)
        if row is None:
            row = OAuthToken(
                user_id=user_id,
                integration=INTEGRATION_NAME,
                access_token="",
                extra_json=json.dumps({"fingerprint": fingerprint}),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            extra = json.loads(row.extra_json or "{}")
            extra["fingerprint"] = fingerprint
            row.extra_json = json.dumps(extra)
            row.updated_at = now
        await session.commit()
        return fingerprint


async def clear_credential(user_id: str) -> None:
    """Remove the persisted credential. Used on hard disconnect."""
    async with db_session_async() as session:
        stmt = sa.delete(OAuthToken).where(
            OAuthToken.user_id == user_id,
            OAuthToken.integration == INTEGRATION_NAME,
        )
        await session.execute(stmt)
        await session.commit()


async def is_connected(user_id: str) -> bool:
    """Return True when a usable AppFolio credential is on file."""
    cred = await load_credential(user_id)
    return cred is not None and bool(cred.jwt)
