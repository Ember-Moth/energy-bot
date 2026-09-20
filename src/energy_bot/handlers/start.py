from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from energy_bot.keyboards.main_menu import main_menu_kb

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = message.from_user.full_name if message.from_user else "朋友"
    await message.answer(
        f"你好,<b>{name}</b>!我是 energy-bot ⚡",
        reply_markup=main_menu_kb,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer("可用命令:\n/start - 开始对话\n/help - 查看帮助")
