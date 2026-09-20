"""handler 层测试:HTML 转义与主菜单按钮(mock 掉 Telegram 与数据库对象)。"""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.handlers import start as start_handler
from energy_bot.handlers.echo import echo


def _message(text: str = "", full_name: str = "测试") -> Message:
    message = AsyncMock()
    message.text = text
    message.from_user = SimpleNamespace(id=1, full_name=full_name, language_code="zh")
    return cast(Message, message)


async def test_echo_escapes_html() -> None:
    message = _message(text="<b>你好</b> & 再会")
    await echo(message)
    cast(AsyncMock, message.answer).assert_awaited_once_with("&lt;b&gt;你好&lt;/b&gt; &amp; 再会")


async def test_start_escapes_full_name() -> None:
    message = _message(full_name="<u>名字</u>&Co")
    session = cast(AsyncSession, AsyncMock())
    await start_handler.cmd_start(message, session)
    cast(AsyncMock, message.answer).assert_awaited_once_with(
        "你好,<b>&lt;u&gt;名字&lt;/u&gt;&amp;Co</b>!我是 energy-bot ⚡",
        reply_markup=start_handler.main_menu_kb,
    )


async def test_help_button_replies_help_text() -> None:
    message = _message(text=start_handler.BTN_HELP)
    await start_handler.btn_help(message)
    cast(AsyncMock, message.answer).assert_awaited_once_with(start_handler.HELP_TEXT)


async def test_status_button_checks_database() -> None:
    message = _message(text=start_handler.BTN_STATUS)
    session = cast(AsyncSession, AsyncMock())
    await start_handler.btn_status(message, session)
    cast(AsyncMock, session.execute).assert_awaited_once()
    cast(AsyncMock, message.answer).assert_awaited_once_with("✅ 服务正常,数据库连接可用。")
