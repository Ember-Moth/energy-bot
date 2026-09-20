"""orders 表访问。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import Order, OrderStatus

DEFAULT_PAGE_SIZE = 20


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    recipient_address: str,
    energy_amount: int,
    duration_hours: int,
    price: Decimal,
    status: OrderStatus = OrderStatus.DRAFT,
) -> Order:
    order = Order(
        user_id=user_id,
        recipient_address=recipient_address,
        energy_amount=energy_amount,
        duration_hours=duration_hours,
        price=price,
        status=status,
    )
    session.add(order)
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


async def get_order_for_update(session: AsyncSession, order_id: int) -> Order | None:
    """行锁取单:支付回调、后台任务与 handler 并发触碰同一订单时用它。"""
    stmt = select(Order).where(Order.id == order_id).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_upstream_order_id(
    session: AsyncSession,
    upstream_order_id: str,
    *,
    for_update: bool = False,
) -> Order | None:
    """按上游单号对账(回调匹配采购结果)。"""
    stmt = select(Order).where(Order.upstream_order_id == upstream_order_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_by_user(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())
