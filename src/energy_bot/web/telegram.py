"""Telegram 更新接收端点。"""

from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web


class TransactionalRequestHandler(SimpleRequestHandler):
    """等待更新处理完成;不使用会提前应答并转后台的 webhook 超时包装。"""

    async def _handle_request(self, bot: Bot, request: web.Request) -> web.Response:
        result = await self.dispatcher.feed_raw_update(
            bot,
            await request.json(loads=bot.session.json_loads),
            **self.data,
        )
        method = result if isinstance(result, TelegramMethod) else None
        return web.Response(body=self._build_response_writer(bot=bot, result=method))


def register_telegram_routes(
    app: web.Application,
    dispatcher: Dispatcher,
    bot: Bot,
    path: str,
    secret_token: str,
) -> None:
    """注册 webhook 端点;请求头 X-Telegram-Bot-Api-Secret-Token 校验失败直接 401。"""
    TransactionalRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=secret_token,
        handle_in_background=False,
    ).register(app, path=path)
