from aiogram import F, Router
from aiogram.types import Message

router = Router(name="echo")


@router.message(F.text)
async def echo(message: Message) -> None:
    text = message.text
    if text is None:  # F.text 过滤器已保证非空,此处仅为类型收窄
        return
    await message.answer(text)
