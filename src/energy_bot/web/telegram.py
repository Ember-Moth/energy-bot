"""Telegram 更新接收端点。"""

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web


def register_telegram_routes(
    app: web.Application,
    dispatcher: Dispatcher,
    bot: Bot,
    path: str,
    secret_token: str,
) -> None:
    """注册 webhook 端点;请求头 X-Telegram-Bot-Api-Secret-Token 校验失败直接 401。"""
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=secret_token,
    ).register(app, path=path)
