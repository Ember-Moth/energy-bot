"""能量租赁:订单状态机与工作流。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.config import TIMEZONE
from energy_bot.models import Order, OrderStatus
from energy_bot.repositories import orders as order_repo

# T 开头 + 33 个 base58 字符(排除 0OIl)
TRON_ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

# 允许的状态跃迁;终态不再迁出
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset({OrderStatus.PAID}),
    OrderStatus.PAID: frozenset({OrderStatus.DELEGATING, OrderStatus.REFUNDED}),
    OrderStatus.DELEGATING: frozenset(
        {OrderStatus.ACTIVE, OrderStatus.FAILED, OrderStatus.REFUNDED}
    ),
    OrderStatus.ACTIVE: frozenset({OrderStatus.EXPIRED}),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.FAILED: frozenset(),
    OrderStatus.REFUNDED: frozenset(),
}


class RentalError(ValueError):
    """业务规则违反。"""


class InvalidTransitionError(RentalError):
    """非法状态跃迁。"""


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def ensure_transition(order: Order, to: OrderStatus) -> None:
    """校验状态跃迁是否合法,非法即抛 InvalidTransitionError。"""
    if to not in ALLOWED_TRANSITIONS[order.status]:
        raise InvalidTransitionError(
            f"订单 {order.id} 不允许从 {order.status.value} 转为 {to.value}"
        )


async def place_order(
    session: AsyncSession,
    *,
    user_id: int,
    recipient_address: str,
    energy_amount: int,
    duration_hours: int,
    price: Decimal,
) -> Order:
    """创建待支付订单(draft)。"""
    if not TRON_ADDRESS_RE.fullmatch(recipient_address):
        raise RentalError(f"无效的 TRON 地址:{recipient_address}")
    if energy_amount <= 0:
        raise RentalError("energy_amount 必须为正整数")
    if duration_hours <= 0:
        raise RentalError("duration_hours 必须为正整数")
    if price <= 0:
        raise RentalError("price 必须为正数")
    return await order_repo.create_order(
        session,
        user_id=user_id,
        recipient_address=recipient_address,
        energy_amount=energy_amount,
        duration_hours=duration_hours,
        price=price,
    )


async def _get_locked(session: AsyncSession, order_id: int) -> Order:
    order = await order_repo.get_order_for_update(session, order_id)
    if order is None:
        raise RentalError(f"订单不存在:{order_id}")
    return order


async def mark_paid(session: AsyncSession, order_id: int) -> Order:
    """收款确认:draft → paid。"""
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.PAID)
    order.status = OrderStatus.PAID
    await session.flush()
    return order


async def start_delegation(
    session: AsyncSession,
    order_id: int,
    *,
    provider: str,
    upstream_order_id: str,
) -> Order:
    """已收款订单提交上游采购:paid → delegating,记录上游单号。"""
    if not provider or not upstream_order_id:
        raise RentalError("provider 与 upstream_order_id 均不能为空")
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.DELEGATING)
    order.status = OrderStatus.DELEGATING
    order.provider = provider
    order.upstream_order_id = upstream_order_id
    order.delegated_at = _now()
    await session.flush()
    return order


async def activate(session: AsyncSession, order_id: int, *, upstream_txid: str = "") -> Order:
    """能量到账:delegating → active,租期从到账时刻起算。"""
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.ACTIVE)
    order.status = OrderStatus.ACTIVE
    if upstream_txid:
        order.upstream_txid = upstream_txid
    if order.expires_at is None:
        order.expires_at = _now() + timedelta(hours=order.duration_hours)
    await session.flush()
    return order


async def expire(session: AsyncSession, order_id: int) -> Order:
    """租期结束:active → expired(到期回收的后台任务调用)。"""
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.EXPIRED)
    order.status = OrderStatus.EXPIRED
    await session.flush()
    return order


async def fail(session: AsyncSession, order_id: int) -> Order:
    """上游执行失败:delegating → failed。"""
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.FAILED)
    order.status = OrderStatus.FAILED
    await session.flush()
    return order


async def refund(session: AsyncSession, order_id: int) -> Order:
    """退款:paid / delegating → refunded。"""
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.REFUNDED)
    order.status = OrderStatus.REFUNDED
    await session.flush()
    return order
