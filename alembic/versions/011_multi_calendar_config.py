"""Allow multiple calendars per user.

Drop the old unique constraint on (user_id, provider) and replace with
(user_id, provider, calendar_id) so each user can enable multiple calendars.

Revision ID: 011
Revises: 010
Create Date: 2026-03-30
"""

from alembic import op

revision: str = "011"
down_revision: str = "010"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_calendar_config_user_provider", "calendar_configs", type_="unique")
    op.create_unique_constraint(
        "uq_calendar_config_user_provider_calendar",
        "calendar_configs",
        ["user_id", "provider", "calendar_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_calendar_config_user_provider_calendar", "calendar_configs", type_="unique"
    )
    op.create_unique_constraint(
        "uq_calendar_config_user_provider",
        "calendar_configs",
        ["user_id", "provider"],
    )
