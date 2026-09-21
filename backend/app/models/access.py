"""Registration gating, admin API keys, and the admin audit log."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base


class AllowedEmail(Base):
    """Pre-approved email addresses that are allowed to register.

    When ``REGISTRATION_MODE`` is "restricted", only emails in this table
    (plus ``ADMIN_EMAIL``) can create new accounts.
    """

    __tablename__ = "allowed_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Placeholder used when a signup does not supply a name. Must stay in sync
# with the ``server_default`` on the ``name`` column, since both router-side
# normalization and the email-template greeter check for this exact literal
# to decide whether to fall back to the neutral ``Hi there,`` salutation.
WAITLIST_NAME_DEFAULT = "user"


class WaitlistEntry(Base):
    """Email addresses of users who want access but aren't yet approved.

    Entries are created via the public ``POST /api/waitlist/join`` endpoint
    and reviewed by admins in the Waitlist tab.
    """

    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default=WAITLIST_NAME_DEFAULT
    )
    # Free-text answer to "what would you use this for?" so admins can tell
    # tradespeople from devs poking the form. Optional, nullable.
    use_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="homepage", server_default="homepage")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminApiKey(Base):
    """A long-lived API key bound to an admin user.

    Long-lived bearer tokens that admin users mint to authenticate from a
    CLI / curl context where an OAuth-issued JWT isn't a fit (interactive
    flow, short expiry).

    Lookup is by ``key_hash`` (SHA-256 hex of the cleartext token). The
    cleartext is shown once at mint time and never stored; if a user loses
    their key they have to mint a new one.

    Scoped to the admin role: the auth path re-checks the owner's
    ``Subscription.role`` on every request, so demoting an admin
    invalidates all their keys instantly without revoking them first.
    ``revoked_at`` covers the explicit-revoke case for admins who stay
    admin but want to retire one device's key.
    """

    __tablename__ = "admin_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Human label set by the admin at mint time ("laptop", "ci runner").
    # Free-form, truncated to 200 chars at the service layer.
    label: Mapped[str] = mapped_column(String(200), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # First 11 chars of the cleartext, including the ``ck_`` family marker
    # plus 8 random suffix chars (``ck_a1b2c3d4``). Stored in this format so
    # the displayed value matches the leading characters of the cleartext
    # the admin pasted; no "strip ck_" mental step. Display-only; the full
    # token is never recoverable.
    key_prefix: Mapped[str] = mapped_column(String(16), default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Stamped on each successful auth that uses this key. NULL means "never
    # used since mint". Best-effort: the auth path updates this in a
    # fire-and-forget background commit so a slow update doesn't block the
    # request.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = active. The auth path treats any non-null value as "denied",
    # regardless of how far in the past it was set, so revocation is
    # irreversible.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminAuditLog(Base):
    """Append-only record of admin reads and mutations against user data.

    Limits the blast radius of a compromised admin token and gives a query
    hook for "show me everything admin X did last week."

    Two generations of fields coexist. ``endpoint`` is the original
    free-text "GET /api/admin/users/{id}" string, still populated for
    forensic queries that predate the structured fields. ``action`` is its
    canonical replacement: short, indexed, and stable across endpoint URL
    refactors. The structured fields are all nullable because rows written
    before they existed only have ``endpoint``.
    """

    __tablename__ = "admin_audit_logs"
    # Serves the "what happened, most recent first" query the admin log
    # view issues. Created by revision 041; declared here so autogenerate
    # does not read it as deleted and propose dropping it.
    __table_args__ = (Index("ix_admin_audit_logs_action_created", "action", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    admin_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
