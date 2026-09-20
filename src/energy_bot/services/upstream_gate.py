"""每进程连接并发 + PostgreSQL 跨进程商户限流,等待期间不占数据库连接。"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import RentalSettings
from energy_bot.models import UpstreamThrottle
from energy_bot.services.upstream.metadata import RateLimitSnapshot


class UpstreamDeferred(RuntimeError):
    """请求尚未发出;将长等待交回持久队列,避免耗尽工作器租约。"""

    def __init__(self, seconds: float):
        super().__init__("上游额度暂不可用")
        self.retry_after = max(1, math.ceil(seconds))


class UpstreamGate:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], scope: str, settings: RentalSettings
    ):
        self.factory = factory
        self.scope = scope
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.upstream_concurrency)

    async def _delay(self, creation: bool) -> float:
        async with self.factory() as session, session.begin():
            await session.execute(
                insert(UpstreamThrottle)
                .values(scope=self.scope)
                .on_conflict_do_nothing(index_elements=[UpstreamThrottle.scope])
            )
            row = (
                await session.scalars(
                    select(UpstreamThrottle)
                    .where(
                        UpstreamThrottle.scope == self.scope,
                    )
                    .with_for_update()
                )
            ).one()
            now = await session.scalar(select(func.clock_timestamp()))
            assert now is not None
            times = [now, row.request_at or now, row.blocked_until or now]
            if creation:
                times.extend((row.order_at or now, row.orders_blocked_until or now))
            delay = (max(times) - now).total_seconds()
            if delay <= 0:
                row.request_at = now + timedelta(
                    seconds=1 / self.settings.upstream_requests_per_second
                )
                if creation:
                    row.order_at = now + timedelta(
                        seconds=1 / self.settings.upstream_orders_per_second
                    )
            return max(0, delay)

    async def _defer(self, seconds: float, *, scope: str | None = None) -> None:
        async with self.factory() as session, session.begin():
            row = (
                await session.scalars(
                    select(UpstreamThrottle)
                    .where(
                        UpstreamThrottle.scope == self.scope,
                    )
                    .with_for_update()
                )
            ).one()
            now = await session.scalar(select(func.clock_timestamp()))
            assert now is not None
            if scope == "orders":
                row.orders_blocked_until = max(
                    row.orders_blocked_until or now, now + timedelta(seconds=seconds)
                )
            else:
                row.blocked_until = max(row.blocked_until or now, now + timedelta(seconds=seconds))

    async def observe(self, snapshot: RateLimitSnapshot) -> None:
        payload = {key: value for key, value in asdict(snapshot).items() if value is not None}
        if not payload:
            return
        async with self.factory() as session, session.begin():
            await session.execute(
                insert(UpstreamThrottle)
                .values(scope=self.scope)
                .on_conflict_do_nothing(index_elements=[UpstreamThrottle.scope])
            )
            row = (
                await session.scalars(
                    select(UpstreamThrottle)
                    .where(
                        UpstreamThrottle.scope == self.scope,
                    )
                    .with_for_update()
                )
            ).one()
            row.rate_snapshot = {**(row.rate_snapshot or {}), **payload}
            row.snapshot_at = await session.scalar(select(func.clock_timestamp()))

    async def run[T](self, request: Callable[[], Awaitable[T]], *, creation: bool) -> T:
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=5)
        except TimeoutError as exc:
            raise UpstreamDeferred(1) from exc
        try:
            while (delay := await self._delay(creation)) > 0:
                if delay > 1:
                    raise UpstreamDeferred(delay)
                await asyncio.sleep(delay)
            try:
                return await request()  # 等到额度后才生成本次签名时间戳和 nonce
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                if isinstance(retry_after, int) and retry_after > 0:
                    snapshot = getattr(exc, "rate_limits", None)
                    scope = (
                        snapshot.scope
                        if isinstance(snapshot, RateLimitSnapshot)
                        and getattr(exc, "status", None) == 429
                        and getattr(exc, "code", None) == "RATE_LIMITED"
                        else None
                    )
                    await self._defer(retry_after, scope=scope)
                raise
        finally:
            self.slots.release()


class TronowGate(UpstreamGate):
    """共享商户滚动 1 秒窗口;读占 API 配额,订单/激活写请求占两种配额。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        scope: str,
        settings: RentalSettings,
        *,
        request_limit: int = 50,
        order_limit: int = 10,
    ):
        super().__init__(factory, scope, settings)
        self.request_limit = request_limit
        self.order_limit = order_limit

    async def _delay(self, creation: bool) -> float:
        async with self.factory() as session, session.begin():
            await session.execute(
                insert(UpstreamThrottle)
                .values(scope=self.scope)
                .on_conflict_do_nothing(index_elements=[UpstreamThrottle.scope])
            )
            row = (
                await session.scalars(
                    select(UpstreamThrottle)
                    .where(
                        UpstreamThrottle.scope == self.scope,
                    )
                    .with_for_update()
                )
            ).one()
            now = await session.scalar(select(func.clock_timestamp()))
            assert now is not None
            cutoff = now - timedelta(seconds=1)
            requests = [at for at in row.request_history if at > cutoff]
            orders = [at for at in row.order_history if at > cutoff]
            snapshot = row.rate_snapshot or {}
            request_limit = min(
                self.request_limit, snapshot.get("request_limit") or self.request_limit
            )
            order_limit = min(self.order_limit, snapshot.get("order_limit") or self.order_limit)
            until = row.blocked_until or now
            if len(requests) >= request_limit:
                until = max(until, requests[-request_limit] + timedelta(seconds=1))
            if creation:
                until = max(until, row.orders_blocked_until or now)
                if len(orders) >= order_limit:
                    until = max(until, orders[-order_limit] + timedelta(seconds=1))
            delay = max(0, (until - now).total_seconds())
            if delay == 0:
                requests.append(now)
                if creation:
                    orders.append(now)
            row.request_history = requests
            row.order_history = orders
            return delay
