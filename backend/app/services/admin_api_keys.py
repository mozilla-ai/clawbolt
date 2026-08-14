"""Mint, validate, and revoke admin API keys.

Cleartext tokens are ``ck_`` + 32 random url-safe bytes. The cleartext
is shown to the admin once at mint time and never persisted; the row
stores ``key_hash`` (SHA-256 hex of the cleartext) plus an
11-character ``key_prefix`` (the ``ck_`` family marker plus 8 random
suffix chars) for display.

The auth path looks up by hash, checks revocation, checks the
owner's role at request time (so demoting an admin instantly kills
all their keys), and returns the owning :class:`User`. Lookup is a
single indexed point query; the prefix only exists to help admins
recognize their own keys in a list.

This module deliberately does not couple to FastAPI; the request
context (auth dispatch, audit logging) lives in the auth resolver and
the admin router.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import secrets

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import AsyncSessionLocal
from backend.app.models import AdminApiKey, Subscription, User

logger = logging.getLogger(__name__)


_TOKEN_FAMILY_PREFIX = "ck_"
# Leading slice of the cleartext stored on the row for display: the
# ``ck_`` family marker plus 8 random suffix chars. Including the
# marker means the displayed value matches the start of the cleartext
# the admin pasted into a CLI, with no "strip the prefix" mental step.
_DISPLAY_PREFIX_LEN = len(_TOKEN_FAMILY_PREFIX) + 8

# Cap on simultaneously-active (un-revoked) keys per admin. Defense
# in depth: an admin who reaches this cap has to revoke a forgotten
# key before minting a new one, which keeps the per-admin attack
# surface bounded. Picked to be high enough that real workflows
# (laptop + ci + ad-hoc sandbox + buffer) never hit it, and low
# enough that an admin with 50 forgotten keys lying around shows up
# as a clear "you should clean up" signal.
ACTIVE_KEY_CAP_PER_ADMIN = 10


class TooManyActiveKeysError(Exception):
    """Raised when an admin tries to mint past ``ACTIVE_KEY_CAP_PER_ADMIN``.

    The route translates this into a 409 with an actionable message;
    callers can also catch it to drive a "revoke first" flow without
    parsing HTTP status codes.
    """

    def __init__(self, *, owner_user_id: str, active_count: int, cap: int) -> None:
        self.owner_user_id = owner_user_id
        self.active_count = active_count
        self.cap = cap
        super().__init__(
            f"admin {owner_user_id} already has {active_count} active keys "
            f"(cap {cap}); revoke one before minting a new one"
        )


def is_api_key_token(token: str) -> bool:
    """Return True if *token* looks like an admin API key.

    Cheap pure-string check used by the auth dispatch to decide
    whether to drive the API-key lookup or fall through to the JWT
    decoder. A misbehaving JWT that happened to start with ``ck_``
    would still fail the hash lookup and 401, so the worst case of
    a wrong dispatch is one extra DB round trip.
    """
    return token.startswith(_TOKEN_FAMILY_PREFIX)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest used as the storage key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token_material() -> tuple[str, str, str]:
    """Generate fresh cleartext, hash, and display prefix for a new key.

    Factored out so token generation stays separate from the DB write
    path. Pure CPU; safe to call from any context.
    """
    # 32 random bytes via secrets.token_urlsafe -> ~43 chars of
    # base64url, prepended with the family marker for ~46 chars total.
    # token_urlsafe is the right primitive: rejection-sampled, no
    # padding, URL-safe so admins can paste from copy-paste contexts
    # that mangle ``+`` and ``/``.
    suffix = secrets.token_urlsafe(32)
    cleartext = f"{_TOKEN_FAMILY_PREFIX}{suffix}"
    key_hash = hash_token(cleartext)
    key_prefix = cleartext[:_DISPLAY_PREFIX_LEN]
    return cleartext, key_hash, key_prefix


async def _lock_owner_keyspace(db: AsyncSession, owner_user_id: str) -> None:
    """Serialize per-owner key minting inside the surrounding transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"admin_api_keys:{owner_user_id}"},
    )


async def mint_api_key(
    db: AsyncSession,
    *,
    owner_user_id: str,
    label: str,
) -> tuple[AdminApiKey, str]:
    """Generate a new key for *owner_user_id*, persist the hash, return both.

    The returned tuple is ``(row, cleartext_token)``. The cleartext is
    the only time the token is visible: the caller must hand it back
    to the admin in the response. Subsequent reads of the row will
    only have ``key_hash`` and ``key_prefix``.

    The caller is responsible for verifying that *owner_user_id*
    refers to a current admin; this function does not check role so
    the admin router can mint keys for itself or for another admin
    without coupling to ``get_current_admin``.

    Raises ``TooManyActiveKeysError`` when the owner already has
    ``ACTIVE_KEY_CAP_PER_ADMIN`` un-revoked keys. Revoked keys do not
    count toward the cap, so an admin who has rotated keys many times
    over the years can still mint as long as old ones are revoked.
    """
    await _lock_owner_keyspace(db, owner_user_id)
    active_count = (
        await db.execute(
            select(func.count(AdminApiKey.id)).where(
                AdminApiKey.user_id == owner_user_id,
                AdminApiKey.revoked_at.is_(None),
            )
        )
    ).scalar_one()
    if active_count >= ACTIVE_KEY_CAP_PER_ADMIN:
        raise TooManyActiveKeysError(
            owner_user_id=owner_user_id,
            active_count=active_count,
            cap=ACTIVE_KEY_CAP_PER_ADMIN,
        )

    cleartext, key_hash, key_prefix = _new_token_material()

    row = AdminApiKey(
        user_id=owner_user_id,
        label=(label or "")[:200],
        key_hash=key_hash,
        key_prefix=key_prefix,
    )
    # ``AsyncSession.add`` stays sync (identity-map ops do not await).
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, cleartext


async def authenticate_api_key(token: str) -> User | None:
    """Resolve a cleartext API key to its owning admin User, or None.

    Returns None on any of:
    * No matching ``key_hash``.
    * The row's ``revoked_at`` is set.
    * The owning user is missing or inactive.
    * The owning user is not currently an admin (Subscription.role !=
      'admin'). This re-check at request time is the load-bearing
      guarantee that demoting an admin kills all their keys without
      needing a sweep over the keys table.

    Side effect: stamps ``last_used_at`` on the row when auth
    succeeds. Best-effort: a failure to commit the timestamp does
    not block the auth response.

    Async because the caller (``auth.session_auth.resolve_multi_user``) is async
    and this helper owns its own session.
    """
    if not is_api_key_token(token):
        return None

    key_hash = hash_token(token)
    db = AsyncSessionLocal()
    try:
        row = (
            await db.execute(select(AdminApiKey).where(AdminApiKey.key_hash == key_hash))
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return None
        user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
        if user is None or not user.is_active:
            return None
        sub = (
            await db.execute(select(Subscription).where(Subscription.user_id == user.id))
        ).scalar_one_or_none()
        if sub is None or sub.role != "admin":
            return None
        # Detach the User from the session before stamping last_used_at.
        # ``AsyncSessionLocal`` defaults to ``expire_on_commit=False`` so
        # this is not strictly required, but expunging early keeps the
        # detached ``User`` safe against any future refresh on this session.
        db.expunge(user)
        try:
            row.last_used_at = datetime.datetime.now(datetime.UTC)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.warning("Failed to stamp last_used_at on api key id=%s", row.id)
        return user
    finally:
        await db.close()


async def revoke_api_key(
    db: AsyncSession, *, key_id: int, owner_user_id: str | None = None
) -> bool:
    """Mark a key revoked. Returns True if the row existed and was revoked.

    When *owner_user_id* is provided, only revoke keys owned by that
    user. The admin router uses that to scope a self-revoke to the
    caller's own keys; a future "force-revoke any admin's key"
    surface can call without the filter.
    """
    stmt = select(AdminApiKey).where(AdminApiKey.id == key_id)
    if owner_user_id is not None:
        stmt = stmt.where(AdminApiKey.user_id == owner_user_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return False
    if row.revoked_at is None:
        row.revoked_at = datetime.datetime.now(datetime.UTC)
        await db.commit()
    return True
