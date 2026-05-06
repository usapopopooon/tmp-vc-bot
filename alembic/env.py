"""Alembic environment configuration."""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from src.database.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: Bot プロセス内から alembic を呼ぶと、
    # デフォルトの True ではアプリ側の root logger が無効化され、以降の
    # logger.info/error がすべて黙殺されてしまう。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 環境変数 DATABASE_URL があれば alembic.ini の設定を上書き
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # alembic は同期接続を使用するため、asyncpg ドライバを削除
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    elif database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    config.set_main_option("sqlalchemy.url", database_url)
else:
    # 環境変数がなければ settings からデフォルト URL を使用
    from src.config import settings

    sync_url = settings.async_database_url.replace("+asyncpg", "")
    config.set_main_option("sqlalchemy.url", sync_url)

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
