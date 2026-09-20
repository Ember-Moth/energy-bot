"""余额租赁入口:产品价格来自服务端配置,不接受用户报价。"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.config import RentalSettings
from energy_bot.models import Order, Wallet
from energy_bot.repositories import orders, users
from energy_bot.services import rental
from energy_bot.services.wallet import WalletError

router = Router(name="rental")
_STATUS_TEXT = {
    "draft": "待处理",
    "paid": "待采购",
    "reserved": "余额已冻结，等待采购",
    "delegating": "采购确认中",
    "active": "已到账",
    "expired": "已到期",
    "failed": "采购失败",
    "refunded": "已退回余额",
}


def _private(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


def _summary(order: Order) -> str:
    minutes = order.duration_minutes or (order.duration_hours or 0) * 60
    return (
        f"#{order.id} · {_STATUS_TEXT[order.status.value]}\n"
        f"能量 {order.energy_amount} / {minutes} 分钟 / {order.price:f} TRX"
    )


@router.message(Command("rent"))
async def rent(
    message: Message, command: CommandObject, session: AsyncSession, rental_settings: RentalSettings
) -> None:
    if not _private(message):
        await message.answer("请私聊机器人办理租赁。")
        return
    if not rental_settings.enabled:
        await message.answer("租赁服务尚未开放。")
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        products = "\n".join(
            f"{p.energy_amount} 能量 / {p.duration_minutes} 分钟：{p.price_trx:f} TRX"
            for p in rental_settings.products
        )
        await message.answer((products or "暂未上架套餐。") + "\n下单格式：/rent 能量数量 TRON地址")
        return
    try:
        energy = int(parts[0])
    except ValueError:
        await message.answer("能量数量必须是整数。")
        return
    # 租期不由用户选择:按能量数量匹配套餐,使用其租期(产品约定为最短租期)
    product = next((p for p in rental_settings.products if p.energy_amount == energy), None)
    if product is None:
        await message.answer("该能量数量未上架,请发送 /rent 查看可选套餐。")
        return
    assert message.from_user is not None
    await users.upsert_user(
        session,
        user_id=message.from_user.id,
        first_name=message.from_user.full_name,
        language_code=message.from_user.language_code or "",
    )
    try:
        async with session.begin_nested():
            order = await rental.reserve_order(
                session,
                user_id=message.from_user.id,
                request_key=f"tg:{message.chat.id}:{message.message_id}",
                recipient_address=parts[1],
                energy_amount=energy,
                duration_minutes=product.duration_minutes,
                price=product.price_trx,
                max_cost=product.max_cost_trx,
            )
    except (rental.RentalError, WalletError) as exc:
        await message.answer(str(exc))
        return
    await session.commit()  # 持久化冻结与任务后才发送受理回复
    await message.answer(_summary(order) + "\n可使用 /order 订单号 查询进度。")


@router.message(Command("balance"))
async def balance(message: Message, session: AsyncSession) -> None:
    if not _private(message):
        await message.answer("请私聊机器人查询余额。")
        return
    assert message.from_user is not None
    account = await session.get(Wallet, message.from_user.id)
    available, frozen = (account.available, account.frozen) if account else (0, 0)
    await session.commit()
    await message.answer(f"可用余额：{available:f} TRX\n冻结金额：{frozen:f} TRX")


@router.message(Command("orders"))
async def my_orders(message: Message, session: AsyncSession) -> None:
    if not _private(message):
        await message.answer("请私聊机器人查询订单。")
        return
    assert message.from_user is not None
    rows = await orders.list_by_user(session, message.from_user.id)
    await session.commit()
    await message.answer("\n\n".join(_summary(row) for row in rows) or "暂无订单。")


@router.message(Command("order", "cancel_order"))
async def order_detail(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if not _private(message):
        await message.answer("请私聊机器人操作订单。")
        return
    try:
        order_id = int(command.args or "")
    except ValueError:
        await message.answer(f"使用格式：/{command.command} 订单号")
        return
    if not 1 <= order_id <= 2147483647:
        await message.answer("订单号超出范围。")
        return
    assert message.from_user is not None
    order = await session.scalar(
        select(Order).where(
            Order.id == order_id,
            Order.user_id == message.from_user.id,
        )
    )
    if order is None:
        await message.answer("订单不存在。")
        return
    if command.command == "cancel_order":
        try:
            async with session.begin_nested():
                order = await rental.cancel_order(
                    session, user_id=message.from_user.id, order_id=order_id
                )
        except (rental.RentalError, WalletError) as exc:
            await message.answer(str(exc))
            return
    await session.commit()
    expiry = order.expires_at.isoformat() if order.expires_at else "待上游确认"
    await message.answer(
        _summary(order) + f"\n接收地址：{order.recipient_address}\n到期时间：{expiry}"
    )
