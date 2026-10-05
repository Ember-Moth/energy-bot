"""充值单数据访问。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import DepositOrder


async def get_by_order_id_for_update(session: AsyncSession, order_id: str) -> DepositOrder | None:
    """锁定并刷新充值单,避免沿用查网关前的 ORM 快照。"""
    return await session.scalar(
        select(DepositOrder)
        .where(DepositOrder.order_id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
