"""持久化采购任务:事务外请求上游,订单行锁与租约保护结果提交。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

logger = logging.getLogger(__name__)
# 仅首次 POST 的明确拒绝可排除该供应商;恢复阶段的错误不能证明原请求未受理。
_REJECTED_CODES = frozenset(
    {
        "INSUFFICIENT_BALANCE",
        "UNSUPPORTED_PRODUCT",
        "INVALID_ARGUMENT",
        "INVALID_JSON",
        "INVALID_RESOURCE_AMOUNT",
        "INVALID_RECEIVER_ADDRESS",
        "INVALID_DURATION",
        "INVALID_CREDENTIALS",
        "INVALID_SIGNATURE",
        "IP_NOT_ALLOWED",
        "INITIAL_DEPOSIT_REQUIRED",
        "insufficient_balance",
    }
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
    if order.wallet_state in ("held", "captured") and order.status is not OrderStatus.EXPIRED:
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

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                # 任务持久化且租约会过期;不要将可能含凭据的异常原文写日志。
                logger.log(logging.ERROR, "订单工作器本轮异常: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.settings.poll_seconds)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        for _ in range(self.settings.batch_size):
            claim = await self._claim()
            if claim is None:
                break
            await self.process(*claim)
        if self.send_message is not None:
            await self.deliver_notifications()

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
            if order.wallet_state == "captured" and order.expires_at is not None:
                if order.expires_at <= _now():
                    await rental.expire(session, order.id)
                    self._finish(order, None)
                else:
                    self._finish(order, (order.expires_at - _now()).total_seconds())
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
            product = Product(
                order.recipient_address,
                order.energy_amount,
                order.duration_minutes or (order.duration_hours or 0) * 60,
            )
            budget = order.max_cost or order.price

        first_submission = attempt is None
        if attempt is None:
            excluded = {a.provider for a in attempts if a.state in ("failed", "rejected")}
            offer, quote_delay = await self._offer(product, excluded, budget)
            async with self.factory() as session, session.begin():
                order = await self._locked(session, order_id, token)
                if order is None:
                    return
                if order.wallet_state != "held":
                    self._finish(order, None)
                    return
                if offer is None or offer.valid_until <= _now():
                    order.retry_count += 1
                    order.last_error = "NO_AFFORDABLE_OFFER"
                    if order.retry_count >= self.settings.quote_retry_limit:
                        await rental.release_order(session, order, reason="NO_AFFORDABLE_OFFER")
                        self._finish(order, None)
                    else:
                        self._finish(order, max(self.settings.poll_seconds, quote_delay))
                    return
                business_id = "eb-" + uuid4().hex
                attempt = PurchaseAttempt(
                    order_id=order_id,
                    sequence=len(attempts) + 1,
                    provider=offer.provider,
                    business_id=business_id,
                    idempotency_key=business_id,
                    request_body=self.providers[offer.provider].body(product, business_id),
                    quoted_cost=offer.cost,
                    state="submitting",
                )
                session.add(attempt)
                await rental.begin_purchase(session, order, offer.provider)
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
            order.last_error = code[:128]
            order.retry_count += 1
            if first_submission and code in _REJECTED_CODES:
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
            self._finish(order, delay)

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
                if result.state == "expired":
                    await rental.expire(session, order.id)
                    self._finish(order, None)
                else:
                    self._finish(order, 60)
                return
            if result.state == "success":
                await rental.complete_purchase(
                    session, order, cost=result.cost, expires_at=result.expires_at, txid=result.txid
                )
                attempt.state = "succeeded"
                delay = (result.expires_at - _now()).total_seconds() if result.expires_at else 60
                self._finish(order, None if order.status is OrderStatus.EXPIRED else max(0, delay))
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
                    60 if result.state in ("reviewing", "expired") else self.settings.poll_seconds,
                )

    async def deliver_notifications(self) -> None:
        if self.send_message is None:
            return
        for _ in range(self.settings.batch_size):
            async with self.factory() as session, session.begin():
                event = await session.scalar(
                    select(OrderNotification)
                    .where(
                        OrderNotification.sent_at.is_(None),
                        OrderNotification.next_run_at <= _now(),
                        or_(
                            OrderNotification.lease_until.is_(None),
                            OrderNotification.lease_until <= _now(),
                        ),
                    )
                    .order_by(OrderNotification.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if event is None:
                    return
                event.lease_token = token = uuid4().hex
                event.lease_until = _now() + timedelta(seconds=self.settings.lease_seconds)
            sent = False
            try:
                await self.send_message(event.user_id, event.text)
                sent = True
            except Exception as exc:
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
                    row.next_run_at = _now() + timedelta(seconds=60)
                    row.lease_token = None
                    row.lease_until = None
