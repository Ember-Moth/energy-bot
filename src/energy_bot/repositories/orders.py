"""orders 表访问。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import Order

DEFAULT_PAGE_SIZE = 20


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


async def get_order_for_update(session: AsyncSession, order_id: int) -> Order | None:
    """行锁取单:支付回调、后台任务与 handler 并发触碰同一订单时用它。"""
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
