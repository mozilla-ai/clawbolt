"""Convert all TIMESTAMP columns to TIMESTAMPTZ.

All datetime columns were stored as naive UTC via DateTime (TIMESTAMP without
timezone). This migration converts them to TIMESTAMP WITH TIME ZONE so that
PostgreSQL returns timezone-aware datetimes and .isoformat() includes the
+00:00 offset (which JavaScript needs to correctly interpret as UTC).

Revision ID: 007
Revises: 006
Create Date: 2026-03-24
"""

from alembic import op

revision: str = "007"
down_revision: str = "006"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Every (table, column) pair that stores a datetime as TIMESTAMP.
_COLUMNS: list[tuple[str, str]] = [
    ("users", "created_at"),
    ("users", "updated_at"),
    ("channel_routes", "created_at"),
    ("sessions", "created_at"),
    ("sessions", "last_message_at"),
    ("messages", "timestamp"),
    ("media_files", "created_at"),
    ("memory_documents", "created_at"),
    ("memory_documents", "updated_at"),
    ("heartbeat_logs", "created_at"),
    ("idempotency_keys", "created_at"),
    ("llm_usage_logs", "created_at"),
    ("calendar_configs", "created_at"),
]


def upgrade() -> None:
    for table, column in _COLUMNS:
        # AT TIME ZONE 'UTC' tells PostgreSQL the existing naive values are UTC.
        op.execute(
            f'ALTER TABLE {table} ALTER COLUMN "{column}" '
            f"TYPE TIMESTAMPTZ USING \"{column}\" AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN "{column}" TYPE TIMESTAMP')
