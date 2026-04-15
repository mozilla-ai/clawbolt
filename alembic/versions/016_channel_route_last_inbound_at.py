"""Add last_inbound_at to channel_routes for connection verification.

Populated whenever an inbound message resolves against a ChannelRoute, so the
channel picker UI can show a "Verified" state once the user has successfully
sent at least one message through the configured channel.

Revision ID: 016
Revises: 015
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "016"
down_revision: str = "015"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_routes",
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_routes", "last_inbound_at")
