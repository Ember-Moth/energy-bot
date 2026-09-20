"""GMPay 充值:下单编排与回调入账。

资金纪律(与 TRONow 回调同源):
- 入账只经 wallet.credit(reference=f"gmpay:{trade_id}"),同凭证重放幂等;
- 回调金额必须与下单应付金额(expected_amount)精确一致,不符拒绝入账转人工;
- 本层不负责事务提交,由调用方(handler / web 回调)控制。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.config import TIMEZONE
from energy_bot.models import DepositOrder, DepositStatus
from energy_bot.services import wallet
from energy_bot.services.payment.gmpay import GmpayClient, GmpayTransaction


class DepositError(ValueError):
    """充值业务规则违反。"""


def new_order_id(user_id: int) -> str:
    """商户单号:dep-{user_id}-{10 位随机},≤ 32 字符,全平台唯一(撞唯一索引时由调用方重试)。"""
    return f"dep-{user_id}-{secrets.token_hex(5)}"


async def create_deposit(
    session: AsyncSession,
    client: GmpayClient,
    *,
    user_id: int,
    amount_trx: Decimal,
    notify_url: str,
    order_id: str,
) -> DepositOrder:
    """创建充值单并向网关下单;记录网关应付金额(actual_amount)作为展示与入账基准。"""
    if amount_trx <= 0:
        raise DepositError("充值金额必须为正数")
    deposit = DepositOrder(
        user_id=user_id,
        order_id=order_id,
        fiat_amount=amount_trx,
        expected_amount=amount_trx,  # currency=trx 下与下单金额相等;以网关响应复核
        status=DepositStatus.CREATED,
    )
    session.add(deposit)
    await session.flush()  # 先持久化意图,再调网关(与采购同一纪律)

    try:
        tx: GmpayTransaction = await client.create_transaction(
            order_id=order_id,
            amount=amount_trx,
            notify_url=notify_url,
        )
    except Exception:
        deposit.status = DepositStatus.FAILED
        await session.flush()
        raise
    deposit.trade_id = tx.trade_id
    deposit.expected_amount = tx.actual_amount
    deposit.receive_address = tx.receive_address
    await session.flush()
    return deposit


async def get_by_trade_id_for_update(session: AsyncSession, trade_id: str) -> DepositOrder | None:
    """行锁取充值单:回调与轮询并发触碰同一单时用它。"""
    stmt = select(DepositOrder).where(DepositOrder.trade_id == trade_id).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_paid(
    session: AsyncSession,
    deposit: DepositOrder,
    *,
    actual_amount: Decimal,
    token: str,
    block_transaction_id: str,
) -> DepositOrder:
    """回调/轮询确认到账:校验金额币种 → 钱包入账 → 置 paid。已 paid 幂等返回。"""
    if deposit.status is DepositStatus.PAID:
        return deposit
    if deposit.status is not DepositStatus.CREATED:
        raise DepositError(f"充值单 {deposit.order_id} 状态异常:{deposit.status.value}")
    if token.lower() != "trx":
        deposit.status = DepositStatus.FAILED
        await session.flush()
        raise DepositError(f"充值单 {deposit.order_id} 币种不符:{token!r},已转人工核对")
    if actual_amount != deposit.expected_amount:
        deposit.status = DepositStatus.FAILED
        await session.flush()
        raise DepositError(
            f"充值单 {deposit.order_id} 金额不符:"
            f"应付 {deposit.expected_amount} 实到 {actual_amount},已转人工核对"
        )
    if not deposit.trade_id:
        raise DepositError(f"充值单 {deposit.order_id} 缺少网关单号")
    await wallet.credit(
        session,
        user_id=deposit.user_id,
        amount=deposit.expected_amount,
        reference=f"gmpay:{deposit.trade_id}",
    )
    deposit.status = DepositStatus.PAID
    deposit.block_transaction_id = block_transaction_id or None
    deposit.paid_at = datetime.now(TIMEZONE)
    await session.flush()
    return deposit
