import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import make_url, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from energy_bot.config import TIMEZONE, load_settings
from energy_bot.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# DSN 来自应用配置(--config/ENERGY_BOT_CONFIG 指定的 config.yaml 或环境变量覆盖),
# alembic.ini 里不需要也不应该写死连接串。
_settings = load_settings()
try:
    _url = make_url(_settings.database.effective_dsn()).set(drivername="postgresql+asyncpg")
except ValueError as exc:
    raise SystemExit(f"database 配置不完整:{exc}") from exc


# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # URL 对象保留密码,不经过 ConfigParser 的百分号插值。
    connectable = create_async_engine(
        _url,
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"TimeZone": TIMEZONE.key}},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
