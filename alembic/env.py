from logging.config import fileConfig

from sqlalchemy import Column, engine_from_config, pool
from sqlalchemy.sql.schema import SchemaItem

from alembic import context
from backend.app.config import settings
from backend.app.database import Base, sync_database_url
from backend.app.models import (  # noqa: F401
    CalendarConfig,
    ChannelRoute,
    ChatSession,
    HeartbeatLog,
    IdempotencyKey,
    LLMUsageLog,
    MemoryDocument,
    Message,
    OAuthToken,
    ToolConfig,
    User,
)

# Alembic Config object
config = context.config

# Set the SQLAlchemy URL from our app settings, routing through the helper so
# that bare ``postgresql://`` and legacy ``postgresql+psycopg2://`` URLs are
# pinned to the psycopg3 driver (matching the production sync engine).
config.set_main_option("sqlalchemy.url", sync_database_url(settings.database_url))

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Revision 041 adopted the premium schema ahead of its models, which still live
# in clawbolt-premium and cannot be declared in both repos at once. Autogenerate
# compares the database against ``Base.metadata``, so without these filters it
# reports every adopted object as deleted and emits 22 destructive operations
# against live tables. #1510 moves the models here; delete this block with it.
_TABLES_AWAITING_MODELS = frozenset(
    {
        "admin_api_keys",
        "admin_audit_logs",
        "allowed_emails",
        "deleted_user_usage",
        "llm_payload_captures",
        "subscriptions",
        "usage_quotas",
        "waitlist_entries",
    }
)
_USER_COLUMNS_AWAITING_MODELS = frozenset({"last_login_at", "inactivity_warned_at"})
_INDEXES_AWAITING_MODELS = frozenset({"ix_users_last_login_at"})


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    """Hide the model-less objects revision 041 adopted from autogenerate."""
    if type_ == "table":
        return name not in _TABLES_AWAITING_MODELS
    if type_ == "index":
        return name not in _INDEXES_AWAITING_MODELS
    if type_ == "column" and isinstance(obj, Column) and obj.table.name == "users":
        return name not in _USER_COLUMNS_AWAITING_MODELS
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
