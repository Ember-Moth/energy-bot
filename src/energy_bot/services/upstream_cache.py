"""PostgreSQL 短期读缓存:跨进程合并刷新,不在数据库事务中等待网络。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import TIMEZONE
from energy_bot.models import UpstreamCache


@dataclass(frozen=True)
class CachedValue:
    payload: dict[str, Any]
    expires_at: datetime


def cache_key(*parts: object) -> str:
    """只保存摘要;endpoint、凭据及产品维度全部参与隔离。"""
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresCache:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, refresh_seconds: float = 70
    ) -> None:
        self.factory = factory
        self.refresh_seconds = refresh_seconds
        self._flights: dict[str, asyncio.Task[CachedValue]] = {}

    async def get(self, key: str, load: Callable[[], Awaitable[CachedValue]]) -> CachedValue:
        task = self._flights.get(key)
        if task is None:
            task = asyncio.create_task(self._get(key, load))
            self._flights[key] = task
            task.add_done_callback(lambda done: self._done(key, done))
        # 单个订单取消不能取消其他订单共享的刷新。
        return await asyncio.shield(task)

    def _done(self, key: str, task: asyncio.Task[CachedValue]) -> None:
        if self._flights.get(key) is task:
            self._flights.pop(key, None)
        if not task.cancelled():
            task.exception()  # 所有等待者取消时也回收异常

    async def _get(self, key: str, load: Callable[[], Awaitable[CachedValue]]) -> CachedValue:
        async with asyncio.timeout(self.refresh_seconds + 5):
            pause = 0.025
            while True:
                async with self.factory() as session:
                    row = await session.scalar(
                        select(UpstreamCache).where(
                            UpstreamCache.key == key,
                            UpstreamCache.expires_at > func.clock_timestamp(),
                        )
                    )
                    if row is not None and row.payload is not None and row.expires_at is not None:
                        return CachedValue(row.payload, row.expires_at)
                token = uuid4().hex
                async with self.factory() as session, session.begin():
                    claim = (
                        insert(UpstreamCache)
                        .values(
                            key=key,
                            refresh_token=token,
                            refresh_until=func.clock_timestamp()
                            + timedelta(seconds=self.refresh_seconds),
                        )
                        .on_conflict_do_update(
                            index_elements=[UpstreamCache.key],
                            set_={
                                "refresh_token": token,
                                "refresh_until": func.clock_timestamp()
                                + timedelta(seconds=self.refresh_seconds),
                            },
                            where=(
                                or_(
                                    UpstreamCache.expires_at.is_(None),
                                    UpstreamCache.expires_at <= func.clock_timestamp(),
                                )
                                & or_(
                                    UpstreamCache.refresh_until.is_(None),
                                    UpstreamCache.refresh_until <= func.clock_timestamp(),
                                )
                            ),
                        )
                        .returning(UpstreamCache.key)
                    )
                    claimed = await session.scalar(claim)
                if claimed is not None:
                    try:
                        value = await load()
                        async with self.factory() as session, session.begin():
                            published = await session.scalar(
                                update(UpstreamCache)
                                .where(
                                    UpstreamCache.key == key,
                                    UpstreamCache.refresh_token == token,
                                )
                                .values(
                                    payload=value.payload,
                                    expires_at=value.expires_at,
                                    refresh_token=None,
                                    refresh_until=None,
                                )
                                .returning(UpstreamCache.key)
                            )
                        if published is not None:
                            return value
                        # 采购期间已失效或租约已换代,旧刷新结果不得重新写回。
                    except BaseException:
                        async with self.factory() as session, session.begin():
                            await session.execute(
                                update(UpstreamCache)
                                .where(
                                    UpstreamCache.key == key,
                                    UpstreamCache.refresh_token == token,
                                )
                                .values(refresh_token=None, refresh_until=None)
                            )
                        raise
                await asyncio.sleep(pause)
                pause = min(pause * 2, 0.25)

    async def invalidate(self, key: str) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(
                update(UpstreamCache)
                .where(UpstreamCache.key == key)
                .values(
                    payload=None,
                    expires_at=None,
                    refresh_token=None,
                    refresh_until=None,
                )
            )

    async def prune(self) -> None:
        async with self.factory() as session, session.begin():
            cutoff = datetime.now(TIMEZONE) - timedelta(minutes=5)
            await session.execute(
                delete(UpstreamCache).where(
                    UpstreamCache.expires_at < cutoff,
                    or_(
                        UpstreamCache.refresh_until.is_(None),
                        UpstreamCache.refresh_until < func.clock_timestamp(),
                    ),
                )
            )

    async def close(self) -> None:
        tasks = list(self._flights.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._flights.clear()
