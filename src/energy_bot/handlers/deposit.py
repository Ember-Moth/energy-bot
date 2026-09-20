"""TRX 充值入口:金额由用户输入,收款地址与应付金额来自 GMPay 下单响应。"""

from decimal import Decimal, InvalidOperation

from aiogram import Router, html
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import DepositOrder, DepositStatus
from energy_bot.repositories import users
from energy_bot.services import deposit as deposit_service
from energy_bot.services.deposit import DepositError
from energy_bot.services.payment.gmpay import PAY_TOKEN, GmpayApiError, GmpayClient

router = Router(name="deposit")

_STATUS_TEXT = {
    DepositStatus.CREATED: "待支付",
    DepositStatus.PAID: "已入账",
    DepositStatus.EXPIRED: "已过期",
    DepositStatus.FAILED: "异常待核对",
}


def _private(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


def _payment_text(deposit: DepositOrder) -> str:
    return (
        f"充值单 #{deposit.id}\n"
        f"收款地址:<code>{deposit.receive_address}</code>\n"
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
        order = await deposit_service.create_deposit(
            session,
            gmpay_client,
            user_id=message.from_user.id,
            amount_trx=amount,
            notify_url=gmpay_notify_url,
            order_id=deposit_service.new_order_id(message.from_user.id),
        )
    except DepositError as exc:
        await message.answer(html.quote(str(exc)))
        return
    except GmpayApiError:
        await message.answer("支付网关暂时不可用,请稍后再试。")
        return
    await session.commit()  # 持久化充值单与网关单号后才发收款信息
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
    # 回调延迟时的自助对账:主动查网关,已支付则走与回调相同的入账路径
    if order.status is DepositStatus.CREATED and order.trade_id:
        try:
            status = await gmpay_client.check_status(order.trade_id)
        except GmpayApiError:
            status = None
        if status == 2:
            async with session.begin_nested():
                locked = await deposit_service.get_by_trade_id_for_update(session, order.trade_id)
                if locked is not None:
                    try:
                        order = await deposit_service.mark_paid(
                            session,
                            locked,
                            actual_amount=locked.expected_amount,
                            token=PAY_TOKEN,
                            block_transaction_id="",
                        )
                    except DepositError:
                        order = locked
        elif status == 3:
            order.status = DepositStatus.EXPIRED
    await session.commit()
    paid = f"\n入账时间:{order.paid_at.isoformat()}" if order.paid_at else ""
    await message.answer(
        f"充值单 #{order.id} · {_STATUS_TEXT[order.status]}\n"
        f"金额:{order.expected_amount:f} TRX\n"
        f"收款地址:<code>{order.receive_address}</code>{paid}"
    )
