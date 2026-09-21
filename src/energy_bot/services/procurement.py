"""持久化采购任务:事务外请求上游,订单行锁与租约保护结果提交。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from energy_bot.config import TIMEZONE, RentalSettings
from energy_bot.models import Order, OrderNotification, OrderStatus, PurchaseAttempt
from energy_bot.repositories.orders import get_order_for_update
from energy_bot.services import rental
from energy_bot.services.providers import (
    ManualReview,
    Offer,
    Product,
    Provider,
    ProviderMismatch,
    PurchaseResult,
)
from energy_bot.services.upstream.tronbid import TronbidApiError
from energy_bot.services.upstream.tronow import TronowApiError
from energy_bot.services.upstream_cache import PostgresCache

logger = logging.getLogger(__name__)
# 仅首次 POST 的明确拒绝可排除该供应商;恢复阶段的错误不能证明原请求未受理。
_REJECTED_CODES = {
    "INSUFFICIENT_BALANCE": 422,
    "UNSUPPORTED_PRODUCT": 422,
    "INVALID_ARGUMENT": 400,
    "INVALID_JSON": 400,
    "INVALID_RESOURCE_AMOUNT": 400,
    "INVALID_RECEIVER_ADDRESS": 400,
    "INVALID_DURATION": 400,
    "INVALID_CREDENTIALS": 401,
    "INVALID_SIGNATURE": 401,
    "IP_NOT_ALLOWED": 403,
    "INITIAL_DEPOSIT_REQUIRED": 403,
}


def _definite_rejection(exc: Exception) -> bool:
    if isinstance(exc, TronowApiError):
        return exc.code in _REJECTED_CODES and exc.status == _REJECTED_CODES[exc.code]
    return (
        isinstance(exc, TronbidApiError)
        and exc.code == "insufficient_balance"
        and exc.status is not None
        and 400 <= exc.status < 500
        and exc.status not in (409, 429)
    )


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _validate_result(result: PurchaseResult) -> None:
    if (
        not isinstance(result.upstream_id, str)
        or not 1 <= len(result.upstream_id) <= 128
        or not result.cost.is_finite()
        or result.cost < 0
        or result.cost > Decimal("99999999999999.999999")
        or result.cost != result.cost.quantize(Decimal("0.000001"))
    ):
        raise ProviderMismatch("上游单号或成本无效")


async def wake_tronow(session: AsyncSession, upstream_id: str, client_id: object) -> bool:
    """签名回调只唤醒余额订单查单;不直接根据不完整回调结算用户资金。"""
    conditions = [PurchaseAttempt.upstream_order_id == upstream_id]
    if isinstance(client_id, str) and client_id:
        conditions.append(PurchaseAttempt.business_id == client_id)
    attempt = await session.scalar(
        select(PurchaseAttempt).where(
            PurchaseAttempt.provider == "tronow",
            or_(*conditions),
        )
    )
    if attempt is None:
        return False
    order = await get_order_for_update(session, attempt.order_id)
    if order is None or order.wallet_state is None:
        return False
    if order.wallet_state in ("held", "captured"):
        order.next_run_at = _now()
    return True


class OrderWorker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        providers: dict[str, Provider],
        settings: RentalSettings,
        send_message: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        self.factory = factory
        self.providers = providers
        self.settings = settings
        self.send_message = send_message
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.order_slots = asyncio.Semaphore(settings.order_concurrency)
        self.notification_slots = asyncio.Semaphore(settings.notification_concurrency)

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="energy-orders")

    async def close(self) -> None:
        self.stop.set()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        for provider in self.providers.values():
            await provider.close()

    @staticmethod
    async def _join(tasks: list[asyncio.Task]) -> None:
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _consumer(self, once: Callable[[], Awaitable[bool]]) -> None:
        while not self.stop.is_set():
            if not await self._safe_once(once):
                await self._sleep(self.settings.idle_poll_seconds)

    async def _safe_once(self, once: Callable[[], Awaitable[bool]]) -> bool:
        try:
            return await once()
        except Exception as exc:
            logger.log(logging.ERROR, "队列任务异常: %s", type(exc).__name__)
            return False

    async def _maintenance(self) -> None:
        cache = PostgresCache(self.factory)
        while not self.stop.is_set():
            try:
                await cache.prune()
            except Exception as exc:
                logger.warning("缓存清理失败: %s", type(exc).__name__)
            await self._sleep(60)

    async def run(self) -> None:
        # 每个消费者完成后立即领取下一项;没有整批等待最慢订单的屏障。
        tasks = [
            asyncio.create_task(self._consumer(self._order_once))
            for _ in range(self.settings.order_concurrency)
        ]
        if self.send_message is not None:
            tasks.extend(
                asyncio.create_task(self._consumer(self._notification_once))
                for _ in range(self.settings.notification_concurrency)
            )
        tasks.append(asyncio.create_task(self._maintenance()))
        await self._join(tasks)

    async def _drain(self, once: Callable[[], Awaitable[bool]], concurrency: int) -> None:
        budget = iter(range(self.settings.batch_size))

        async def consume() -> None:
            for _ in budget:
                if not await self._safe_once(once):
                    return

        await self._join(
            [
                asyncio.create_task(consume())
                for _ in range(min(concurrency, self.settings.batch_size))
            ]
        )

    async def tick(self) -> None:
        """有界单轮,供测试/维护使用;生产 run 使用连续的独立消费者。"""
        await self._join(
            [
                asyncio.create_task(self._drain(self._order_once, self.settings.order_concurrency)),
                asyncio.create_task(self.deliver_notifications()),
            ]
        )

    async def _order_once(self) -> bool:
        async with self.order_slots:
            claim = await self._claim()
            if claim is None:
                return False
            await self.process(*claim)
            return True

    async def _claim(self) -> tuple[int, str] | None:
        async with self.factory() as session, session.begin():
            now = _now()
            order = await session.scalar(
                select(Order)
                .where(
                    Order.wallet_state.in_(("held", "captured")),
                    Order.status.in_(
                        (OrderStatus.RESERVED, OrderStatus.DELEGATING, OrderStatus.ACTIVE)
                    ),
                    Order.next_run_at <= now,
                    or_(Order.lease_until.is_(None), Order.lease_until <= now),
                )
                .order_by(Order.next_run_at, Order.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if order is None:
                return None
            token = uuid4().hex
            order.lease_token = token
            order.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
            return order.id, token

    async def _locked(self, session: AsyncSession, order_id: int, token: str) -> Order | None:
        order = await get_order_for_update(session, order_id)
        if order is None or order.lease_token != token:
            return None
        return order

    @staticmethod
    def _finish(order: Order, delay: float | None) -> None:
        order.lease_token = None
        order.lease_until = None
        order.next_run_at = None if delay is None else _now() + timedelta(seconds=delay)

    async def _offer(
        self, product: Product, excluded: set[str], budget: Decimal
    ) -> tuple[Offer | None, float]:
        names = [name for name in self.providers if name not in excluded]
        results = await asyncio.gather(
            *(self.providers[n].quote(product) for n in names), return_exceptions=True
        )
        offers = [
            r
            for r in results
            if isinstance(r, Offer)
            and r.provider in names
            and r.cost.is_finite()
            and 0 <= r.cost <= budget
            and r.valid_until > _now()
        ]
        delay = max([getattr(r, "retry_after", None) or 0 for r in results] + [0])
        return (min(offers, key=lambda r: (r.cost, r.provider)) if offers else None, delay)

    async def process(self, order_id: int, token: str) -> None:
        async with self.factory() as session, session.begin():
            order = await self._locked(session, order_id, token)
            if order is None:
                return
            if order.wallet_state not in ("held", "captured"):
                self._finish(order, None)
                return
            if order.wallet_state == "captured":
                # 已结算订单是终态(不再管理上游租期),直接出队完结。
                self._finish(order, None)
                return
            attempts = list(
                (
                    await session.scalars(
                        select(PurchaseAttempt)
                        .where(
                            PurchaseAttempt.order_id == order_id,
                        )
                        .order_by(PurchaseAttempt.sequence)
                    )
                ).all()
            )
            attempt = attempts[-1] if attempts else None
            if attempt is not None and attempt.state in ("failed", "rejected"):
                attempt = None
            if (
                attempt is not None
                and not attempt.upstream_order_id
                and attempt.state != "reviewing"
            ):
                if attempt.submit_attempts >= self.settings.max_submit_attempts:
                    attempt.state = "reviewing"
                    order.last_error = "SUBMIT_RETRY_EXHAUSTED"
                    await rental.notify_order(
                        session,
                        order,
                        "review",
                        f"订单 #{order.id} 提交结果待核对，余额保持冻结，正在查询原单。",
                    )
                else:
                    # 恢复可能先查业务号再重放,在调用前持久化预算,崩溃也不会无限提交。
                    attempt.submit_attempts += 1
            product = Product(
                order.recipient_address,
                order.energy_amount,
                order.duration_minutes,
            )
            budget = order.max_cost or order.price

        first_submission = attempt is None
        if attempt is None:
            excluded = {a.provider for a in attempts if a.state in ("failed", "rejected")}
            # 只按已装配的供应商数量判断模式,不因某次询价失败而退化成直采。
            single_provider = len(self.providers) == 1
            offer = None
            quote_delay = 0.0
            selected = None
            single_failure = "UPSTREAM_EXHAUSTED"
            if single_provider:
                name, adapter = next(iter(self.providers.items()))
                if name not in excluded:
                    if adapter.supports(product):
                        selected = name
                    else:
                        single_failure = "UNSUPPORTED_PRODUCT"
            else:
                offer, quote_delay = await self._offer(product, excluded, budget)
                if offer is not None:
                    selected = offer.provider
            async with self.factory() as session, session.begin():
                order = await self._locked(session, order_id, token)
                if order is None:
                    return
                if order.wallet_state != "held":
                    self._finish(order, None)
                    return
                if selected is None or (offer is not None and offer.valid_until <= _now()):
                    if single_provider:
                        # 本地规格不支持或唯一供应商已明确失败,无需继续询价重试。
                        await rental.release_order(session, order, reason=single_failure)
                        self._finish(order, None)
                        return
                    order.retry_count += 1
                    order.last_error = "NO_AFFORDABLE_OFFER"
                    if order.retry_count >= self.settings.quote_retry_limit:
                        await rental.release_order(session, order, reason="NO_AFFORDABLE_OFFER")
                        self._finish(order, None)
                    else:
                        self._finish(
                            order,
                            max(self.settings.poll_seconds, quote_delay)
                            + secrets.randbelow(1000) / 1000,
                        )
                    return
                business_id = "eb-" + uuid4().hex
                attempt = PurchaseAttempt(
                    order_id=order_id,
                    sequence=len(attempts) + 1,
                    provider=selected,
                    business_id=business_id,
                    idempotency_key=business_id,
                    request_body=self.providers[selected].body(product, business_id),
                    quoted_cost=offer.cost if offer is not None else None,
                    state="submitting",
                    submit_attempts=1,
                )
                session.add(attempt)
                await rental.begin_purchase(session, order, selected)
                # flush + 事务提交完成后才允许 POST;崩溃后读取同一条请求恢复。
                await session.flush()

        provider = self.providers.get(attempt.provider)
        if provider is None:
            await self._error(
                order_id, token, attempt.id, first_submission, ManualReview("该订单供应商未配置")
            )
            return
        try:
            result = await (
                provider.submit(attempt) if first_submission else provider.recover(attempt)
            )
            _validate_result(result)
        except Exception as exc:
            await self._error(order_id, token, attempt.id, first_submission, exc)
            return
        await self._apply(order_id, token, attempt.id, result)

    async def _error(
        self, order_id: int, token: str, attempt_id: int, first_submission: bool, exc: Exception
    ) -> None:
        async with self.factory() as session, session.begin():
            order = await self._locked(session, order_id, token)
            if order is None or order.wallet_state == "released":
                return
            attempt = await session.get(PurchaseAttempt, attempt_id)
            assert attempt is not None
            code = (
                exc.code
                if isinstance(exc, (TronowApiError, TronbidApiError))
                else type(exc).__name__
            )
            attempt.error_code = code[:128]
            if isinstance(exc, (TronowApiError, TronbidApiError)):
                attempt.last_http_status = exc.status
                attempt.last_request_id = getattr(exc, "request_id", None)
                attempt.last_raw_code = getattr(exc, "raw_code", exc.code)
                logger.warning(
                    "上游请求待恢复: order=%s provider=%s status=%s code=%r request_id=%r",
                    order.id,
                    attempt.provider,
                    exc.status,
                    attempt.last_raw_code,
                    attempt.last_request_id,
                )
            order.last_error = code[:128]
            order.retry_count += 1
            if first_submission and _definite_rejection(exc):
                attempt.state = "rejected"
                self._finish(order, self.settings.poll_seconds)
                return
            review = isinstance(exc, (ManualReview, ProviderMismatch)) or (
                isinstance(exc, (TronowApiError, TronbidApiError))
                and exc.status is not None
                and 400 <= exc.status < 500
                and exc.status != 429
            )
            if review:
                attempt.state = "reviewing"
                await rental.notify_order(
                    session,
                    order,
                    "review",
                    f"订单 #{order.id} 正在核对上游结果，余额保持原状态，请勿重复下单。",
                )
                logger.warning("订单需要核对: order=%s provider=%s", order_id, attempt.provider)
            retry_after = getattr(exc, "retry_after", None) or 0
            delay = max(
                retry_after, min(300, self.settings.poll_seconds * 2 ** min(order.retry_count, 6))
            )
            self._finish(order, delay + secrets.randbelow(1000) / 1000)

    async def _apply(
        self, order_id: int, token: str, attempt_id: int, result: PurchaseResult
    ) -> None:
        async with self.factory() as session, session.begin():
            order = await self._locked(session, order_id, token)
            if order is None or order.wallet_state == "released":
                return
            attempt = await session.get(PurchaseAttempt, attempt_id)
            assert attempt is not None
            if (attempt.upstream_order_id and attempt.upstream_order_id != result.upstream_id) or (
                attempt.actual_cost is not None and attempt.actual_cost != result.cost
            ):
                attempt.state = "reviewing"
                order.last_error = "UPSTREAM_IDENTITY_OR_COST_CHANGED"
                await rental.notify_order(
                    session, order, "review", f"订单 #{order.id} 正在核对上游结果。"
                )
                self._finish(order, 60)
                return
            if result.http_status is not None:
                attempt.last_http_status = result.http_status
                attempt.last_request_id = result.request_id
                attempt.last_raw_code = "OK"
                attempt.error_code = None
            attempt.upstream_order_id = result.upstream_id
            attempt.actual_cost = result.cost
            order.upstream_order_id = result.upstream_id
            order.purchase_cost = result.cost
            order.retry_count = 0
            if result.cost > (order.max_cost or order.price):
                order.last_error = "COST_OVERRUN"
                logger.warning("采购锁价高于预算: order=%s", order.id)
            else:
                order.last_error = None
            if order.wallet_state == "captured":
                # 已结算订单是终态,结果仅用于回填采购尝试,不再流转。
                self._finish(order, None)
                return
            if result.state == "success":
                await rental.complete_purchase(session, order, cost=result.cost, txid=result.txid)
                attempt.state = "succeeded"
                self._finish(order, None)
            elif result.state == "failed":
                attempt.state = "failed"
                self._finish(order, self.settings.poll_seconds)
            else:
                attempt.state = (
                    "reviewing" if result.state in ("reviewing", "expired") else "pending"
                )
                if result.state in ("reviewing", "expired"):
                    await rental.notify_order(
                        session, order, "review", f"订单 #{order.id} 正在核对上游结果。"
                    )
                    logger.warning("上游订单需人工核对: order=%s", order.id)
                self._finish(
                    order,
                    max(
                        result.retry_after or 0,
                        60
                        if result.state in ("reviewing", "expired")
                        else self.settings.poll_seconds,
                    ),
                )

    async def deliver_notifications(self) -> None:
        if self.send_message is not None:
            await self._drain(self._notification_once, self.settings.notification_concurrency)

    async def _notification_once(self) -> bool:
        if self.send_message is None:
            return False
        async with self.notification_slots:
            async with self.factory() as session, session.begin():
                earlier = aliased(OrderNotification)
                event = await session.scalar(
                    select(OrderNotification)
                    .where(
                        OrderNotification.sent_at.is_(None),
                        OrderNotification.next_run_at <= _now(),
                        or_(
                            OrderNotification.lease_until.is_(None),
                            OrderNotification.lease_until <= _now(),
                        ),
                        ~select(earlier.id)
                        .where(
                            earlier.order_id == OrderNotification.order_id,
                            earlier.id < OrderNotification.id,
                            earlier.sent_at.is_(None),
                        )
                        .exists(),
                    )
                    .order_by(OrderNotification.next_run_at, OrderNotification.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if event is None:
                    return False
                event.lease_token = token = uuid4().hex
                event.lease_until = _now() + timedelta(seconds=self.settings.lease_seconds)
            sent = False
            retry_delay = 60
            try:
                await self.send_message(event.user_id, event.text)
                sent = True
            except Exception as exc:
                retry_delay = max(60, getattr(exc, "retry_after", None) or 0)
                logger.warning(
                    "订单通知发送失败: notification=%s error=%s", event.id, type(exc).__name__
                )
            async with self.factory() as session, session.begin():
                row = await session.scalar(
                    select(OrderNotification)
                    .where(
                        OrderNotification.id == event.id,
                    )
                    .with_for_update()
                )
                if row is not None and row.lease_token == token:
                    row.sent_at = _now() if sent else None
                    row.next_run_at = _now() + timedelta(seconds=retry_delay)
                    row.lease_token = None
                    row.lease_until = None
            return True
