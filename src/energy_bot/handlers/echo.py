from aiogram import F, Router, html
from aiogram.types import Message

router = Router(name="echo")


@router.message(F.text)
async def echo(message: Message) -> None:
    text = message.text
    if text is None:  # F.text 过滤器已保证非空,此处仅为类型收窄
        return
    # 默认 parse_mode=HTML,用户原文必须转义,否则含 < 等字符会被 Telegram 拒收
    await message.answer(html.quote(text))
