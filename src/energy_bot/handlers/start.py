from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.keyboards.main_menu import main_menu_kb
from energy_bot.repositories import users as user_repo

router = Router(name="start")


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
    name = message.from_user.full_name if message.from_user else "朋友"
    await message.answer(
        f"你好,<b>{name}</b>!我是 energy-bot ⚡",
        reply_markup=main_menu_kb,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer("可用命令:\n/start - 开始对话\n/help - 查看帮助")
