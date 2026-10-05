"""TRX 充值入口:金额由用户输入,收款地址与应付金额来自 GMPay 下单响应。"""

import logging
from decimal import Decimal, InvalidOperation

from aiogram import Router, html
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from aiohttp import ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import DepositOrder, DepositStatus
from energy_bot.repositories import deposits, users
from energy_bot.services import deposit as deposit_service
from energy_bot.services.deposit import DepositError
from energy_bot.services.payment.gmpay import GmpayApiError, GmpayClient

router = Router(name="deposit")
logger = logging.getLogger(__name__)

_STATUS_TEXT = {
    DepositStatus.CREATED: "待支付",
    DepositStatus.PAID: "已入账",
    DepositStatus.EXPIRED: "已过期",
    DepositStatus.FAILED: "异常待核对",
}


def _private(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


def _payment_text(deposit: DepositOrder) -> str:
    if deposit.status is not DepositStatus.CREATED:
        return f"充值单 #{deposit.id} · {_STATUS_TEXT[deposit.status]}"
    if not deposit.trade_id:
        return (
            f"充值单 #{deposit.id} · 下单结果待核对\n"
            "暂未取得收款信息，请勿转账。\n"
            f"可用 /deposit_status {deposit.id} 查询，或联系管理员核对原单。"
        )
    return (
        f"充值单 #{deposit.id}\n"
        f"收款地址:<code>{html.quote(deposit.receive_address)}</code>\n"
        f"应付金额:<b>{deposit.expected_amount:f} TRX</b>\n\n"
        "⚠️ 转账金额必须与上方完全一致,多付少付都不会到账。\n"
        "到账后自动入账,可用 /deposit_status 查询。"
    )


@router.message(Command("deposit"))
async def deposit(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    gmpay_client: GmpayClient | None,
    gmpay_notify_url: str,
) -> None:
    if not _private(message):
        await message.answer("请私聊机器人充值。")
        return
    if gmpay_client is None:
        await message.answer("充值服务尚未开放。")
        return
    args = (command.args or "").split()
    if len(args) != 1:
        await message.answer("使用格式:/deposit TRX数量\n例如:/deposit 50")
        return
    try:
        amount = Decimal(args[0])
    except InvalidOperation:
        await message.answer("金额必须是数字。")
        return
    if not amount.is_finite() or amount <= 0 or amount > Decimal(10_000_000):
        await message.answer("金额必须为正且不超过 10,000,000 TRX。")
        return
    assert message.from_user is not None
    await users.upsert_user(
        session,
        user_id=message.from_user.id,
        first_name=message.from_user.full_name,
        language_code=message.from_user.language_code or "",
    )
    try:
        order, created = await deposit_service.prepare_deposit(
            session,
            user_id=message.from_user.id,
            amount_trx=amount,
            order_id=deposit_service.order_id_for_message(
                message.from_user.id, message.chat.id, message.message_id
            ),
        )
    except DepositError as exc:
        await message.answer(html.quote(str(exc)))
        return
    await session.commit()  # 原单先落库,网关请求期间不持有事务或用户行锁
    if created:
        try:
            order = await deposit_service.submit_deposit(
                session, gmpay_client, order, notify_url=gmpay_notify_url
            )
        except (DepositError, GmpayApiError, ClientError, TimeoutError) as exc:
            logger.warning("充值下单结果待核对: deposit=%s error=%s", order.id, type(exc).__name__)
            # 回调可能已先行入账;重新锁定刷新,不将异常覆盖为 failed。
            refreshed = await deposits.get_by_order_id_for_update(session, order.order_id)
            assert refreshed is not None
            order = refreshed
        await session.commit()
    await message.answer(_payment_text(order))


@router.message(Command("deposit_status"))
async def deposit_status(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    gmpay_client: GmpayClient | None,
) -> None:
    if not _private(message):
        await message.answer("请私聊机器人查询。")
        return
    if gmpay_client is None:
        await message.answer("充值服务尚未开放。")
        return
    try:
        deposit_id = int(command.args or "")
    except ValueError:
        await message.answer("使用格式:/deposit_status 充值单号")
        return
    if not 1 <= deposit_id <= 2147483647:
        await message.answer("充值单号超出范围。")
        return
    assert message.from_user is not None
    order = await session.scalar(
        select(DepositOrder).where(
            DepositOrder.id == deposit_id,
            DepositOrder.user_id == message.from_user.id,
        )
    )
    if order is None:
        await message.answer("充值单不存在。")
        return
    await session.commit()  # 查网关前释放读事务;状态应用必须重新锁定原单
    order = await deposit_service.reconcile_deposit(session, gmpay_client, order)
    await session.commit()
    if not order.trade_id:
        await message.answer(_payment_text(order))
        return
    paid = f"\n入账时间:{order.paid_at.isoformat()}" if order.paid_at else ""
    await message.answer(
        f"充值单 #{order.id} · {_STATUS_TEXT[order.status]}\n"
        f"金额:{order.expected_amount:f} TRX\n"
        f"收款地址:<code>{html.quote(order.receive_address)}</code>{paid}"
    )
