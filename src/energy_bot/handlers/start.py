from aiogram import F, Router, html
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.keyboards.main_menu import BTN_HELP, BTN_STATUS, main_menu_kb
from energy_bot.repositories import users as user_repo

router = Router(name="start")

HELP_TEXT = (
    "可用命令:\n/start - 开始对话\n/help - 查看帮助"
    "\n/rent - 查看套餐与下单\n/balance - 查询余额\n/orders - 最近订单"
    "\n/order 订单号 - 查询详情\n/cancel_order 订单号 - 取消尚未采购的订单"
)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession) -> None:
    if message.from_user:
        # 会话由 DbSessionMiddleware 注入,正常返回自动提交
        await user_repo.upsert_user(
            session,
            user_id=message.from_user.id,
            first_name=message.from_user.full_name,
            language_code=message.from_user.language_code or "",
        )
    # 用户名可含 < > & 等任意字符,拼进 HTML 消息前必须转义
    name = html.quote(message.from_user.full_name) if message.from_user else "朋友"
    await message.answer(
        f"你好,<b>{name}</b>!我是 energy-bot ⚡",
        reply_markup=main_menu_kb,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text == BTN_STATUS)
async def btn_status(message: Message, session: AsyncSession) -> None:
    await session.execute(text("SELECT 1"))  # 数据库探活,失败会抛异常由 aiogram 记录
    await message.answer("✅ 服务正常,数据库连接可用。")
