from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from backend.app.config import settings
from backend.app.database import Base
from backend.app.models import (  # noqa: F401
    Client,
    Contractor,
    Conversation,
    Estimate,
    EstimateLineItem,
    HeartbeatLog,
    MediaFile,
    Memory,
    Message,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = settings.database_url
    connectable = create_engine(url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
