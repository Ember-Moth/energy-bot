"""数据库:async engine 与会话工厂。

模型见 models.py;schema 由 Alembic 管理(alembic/ 目录),启动时不再自动建表。
"""

from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from energy_bot.config import TIMEZONE


def create_engine_from_dsn(dsn: str, *, pool_size: int = 5, max_overflow: int = 10) -> AsyncEngine:
    """postgres:// DSN 统一走 asyncpg 驱动;连接时区固定为项目时区(东八区)。"""
    url = make_url(dsn).set(drivername="postgresql+asyncpg")
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        connect_args={"server_settings": {"TimeZone": TIMEZONE.key}},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
