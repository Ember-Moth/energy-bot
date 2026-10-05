"""GMPay 充值:持久化下单意图、恢复原单与回调入账。

金额及钱包入账在行锁内校验;事务由 handler / web 回调提交。
下单前必须提交 prepare_deposit 的结果,未知结果不得重新 POST。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from aiohttp import ClientError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.config import TIMEZONE
from energy_bot.models import DepositOrder, DepositStatus
from energy_bot.repositories import deposits
from energy_bot.services import wallet
from energy_bot.services.payment.gmpay import PAY_TOKEN, GmpayApiError, GmpayClient
from energy_bot.services.rental import is_valid_tron_address
from energy_bot.services.wallet import WalletError


class DepositError(ValueError):
    """充值业务规则违反。"""


def order_id_for_message(user_id: int, chat_id: int, message_id: int) -> str:
    """同一消息稳定映射到 32 字符商户单号,复用已有唯一约束。"""
    digest = hashlib.sha256(f"{user_id}:{chat_id}:{message_id}".encode()).hexdigest()
    return f"dep-{digest[:28]}"


async def prepare_deposit(
    session: AsyncSession, *, user_id: int, amount_trx: Decimal, order_id: str
) -> tuple[DepositOrder, bool]:
    """幂等记录下单意图;只有首次插入者可以在提交事务后请求网关。"""
    try:
        wallet.validate_amount(amount_trx)
    except WalletError as exc:
        raise DepositError(str(exc)) from exc
    inserted = await session.scalar(
        insert(DepositOrder)
        .values(
            user_id=user_id,
            order_id=order_id,
            fiat_amount=amount_trx,
            expected_amount=amount_trx,
            status=DepositStatus.CREATED,
        )
        .on_conflict_do_nothing(index_elements=[DepositOrder.order_id])
        .returning(DepositOrder.id)
    )
    order = await deposits.get_by_order_id_for_update(session, order_id)
    assert order is not None
    if order.user_id != user_id or order.fiat_amount != amount_trx:
        raise DepositError("相同消息不能用于不同充值金额或用户")
    return order, inserted is not None


def _bind_transaction(
    order: DepositOrder, *, trade_id: str, actual_amount: Decimal, receive_address: str
) -> None:
    if not isinstance(trade_id, str) or not 1 <= len(trade_id) <= 64:
        raise DepositError("网关单号无效,需核对原单")
    try:
        wallet.validate_amount(actual_amount)
    except WalletError as exc:
        raise DepositError("网关应付金额无效,需核对原单") from exc
    if not isinstance(receive_address, str) or not is_valid_tron_address(receive_address):
        raise DepositError("网关收款地址无效,需核对原单")
    if order.trade_id is not None:
        if (order.trade_id, order.expected_amount, order.receive_address) != (
            trade_id,
            actual_amount,
            receive_address,
        ):
            raise DepositError("网关原单身份或金额发生变化,需人工核对")
        return
    order.trade_id = trade_id
    order.expected_amount = actual_amount
    order.receive_address = receive_address


async def submit_deposit(
    session: AsyncSession, client: GmpayClient, order: DepositOrder, *, notify_url: str
) -> DepositOrder:
    """调用方已提交原单;网络异常保留意图,成功响应与回调按同一行锁合并。"""
    tx = await client.create_transaction(
        order_id=order.order_id, amount=order.fiat_amount, notify_url=notify_url
    )
    if tx.order_id != order.order_id or tx.amount != order.fiat_amount:
        raise DepositError("网关返回的商户单号或下单金额不符,需核对原单")
    locked = await deposits.get_by_order_id_for_update(session, order.order_id)
    assert locked is not None
    _bind_transaction(
        locked,
        trade_id=tx.trade_id,
        actual_amount=tx.actual_amount,
        receive_address=tx.receive_address,
    )
    await session.flush()
    return locked


async def match_callback(
    session: AsyncSession,
    *,
    order_id: str,
    trade_id: str,
    amount: Decimal,
    actual_amount: Decimal,
    receive_address: str,
    token: str,
) -> DepositOrder | None:
    """仅在验签后调用;响应丢失时按商户单号恢复网关身份和应付金额。"""
    order = await deposits.get_by_order_id_for_update(session, order_id)
    if order is None:
        return None
    if order.fiat_amount != amount or (order.trade_id and order.trade_id != trade_id):
        raise DepositError("充值回调的原单身份或下单金额不符")
    if order.trade_id is None:
        if token.lower() != PAY_TOKEN:
            raise DepositError("充值回调的币种不符,不能恢复原单")
        _bind_transaction(
            order,
            trade_id=trade_id,
            actual_amount=actual_amount,
            receive_address=receive_address,
        )
    return order


async def reconcile_deposit(
    session: AsyncSession, client: GmpayClient, order: DepositOrder
) -> DepositOrder:
    """查网关后重新锁定并刷新;已入账不被旧结果覆盖,过期单仍可核对付款。"""
    status = None
    payable = (DepositStatus.CREATED, DepositStatus.EXPIRED)
    if order.status in payable and order.trade_id:
        try:
            status = await client.check_status(order.trade_id)
        except GmpayApiError, ClientError, TimeoutError:
            pass
    locked = await deposits.get_by_order_id_for_update(session, order.order_id)
    assert locked is not None
    if status == 2 and locked.status in payable:
        await mark_paid(
            session,
            locked,
            actual_amount=locked.expected_amount,
            token=PAY_TOKEN,
            block_transaction_id="",
        )
    elif status == 3 and locked.status is DepositStatus.CREATED:
        locked.status = DepositStatus.EXPIRED
    return locked


async def mark_paid(
    session: AsyncSession,
    deposit: DepositOrder,
    *,
    actual_amount: Decimal,
    token: str,
    block_transaction_id: str,
) -> DepositOrder:
    """可信支付证据可恢复过期单;已入账仅补交易号,不重复加款或改入账时间。"""
    if deposit.status not in (DepositStatus.CREATED, DepositStatus.EXPIRED, DepositStatus.PAID):
        raise DepositError(f"充值单 {deposit.order_id} 状态异常:{deposit.status.value}")
    if token.lower() != "trx":
        if deposit.status is not DepositStatus.PAID:
            deposit.status = DepositStatus.FAILED
            await session.flush()
        raise DepositError(f"充值单 {deposit.order_id} 币种不符:{token!r},已转人工核对")
    if actual_amount != deposit.expected_amount:
        if deposit.status is not DepositStatus.PAID:
            deposit.status = DepositStatus.FAILED
            await session.flush()
        raise DepositError(
            f"充值单 {deposit.order_id} 金额不符:"
            f"应付 {deposit.expected_amount} 实到 {actual_amount},已转人工核对"
        )
    if not deposit.trade_id:
        raise DepositError(f"充值单 {deposit.order_id} 缺少网关单号")
    if len(block_transaction_id) > 128:
        raise DepositError("充值回调交易号超出范围,需核对支付凭证")
    if (
        block_transaction_id
        and deposit.block_transaction_id
        and block_transaction_id != deposit.block_transaction_id
    ):
        raise DepositError("充值回调交易号与已有凭证不符,需人工核对")
    if deposit.status is DepositStatus.PAID:
        if block_transaction_id and not deposit.block_transaction_id:
            deposit.block_transaction_id = block_transaction_id
            await session.flush()
        return deposit
    await wallet.credit(
        session,
        user_id=deposit.user_id,
        amount=deposit.expected_amount,
        reference=f"gmpay:{deposit.trade_id}",
    )
    deposit.status = DepositStatus.PAID
    deposit.block_transaction_id = block_transaction_id or deposit.block_transaction_id
    deposit.paid_at = deposit.paid_at or datetime.now(TIMEZONE)
    await session.flush()
    return deposit
