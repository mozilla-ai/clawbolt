from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

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


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
