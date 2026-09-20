import asyncio
import logging
import secrets
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from energy_bot.config import load_settings
from energy_bot.handlers import routers
from energy_bot.middlewares.logging import LoggingMiddleware

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

logger = logging.getLogger(__name__)


def _loop_factory() -> Callable[[], AbstractEventLoop] | None:
    """非 Windows 平台使用 uvloop 事件循环。"""
    if sys.platform == "win32":
        return None
    import uvloop

    return uvloop.new_event_loop


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger.info("事件循环: %s", type(asyncio.get_running_loop()).__module__)
    settings = load_settings()
    if not settings.webhook.secret_token:
        settings = replace(
            settings,
            webhook=replace(settings.webhook, secret_token=secrets.token_urlsafe(32)),
        )
        logger.info("webhook.secret_token 未配置,已自动生成(仅本次启动有效)")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    dp.include_routers(*routers)

    async def on_startup(hook_bot: Bot) -> None:
        hook = settings.webhook
        await hook_bot.set_webhook(
            f"{hook.base_url}{hook.path}",
            secret_token=hook.secret_token,
            drop_pending_updates=True,
        )
        logger.info("webhook 已设置: %s%s", hook.base_url, hook.path)

    async def on_shutdown(hook_bot: Bot) -> None:
        await hook_bot.delete_webhook(drop_pending_updates=True)
        await hook_bot.session.close()
        logger.info("webhook 已移除,bot 会话已关闭")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/healthz", health)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook.secret_token,
    ).register(app, path=settings.webhook.path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    hook = settings.webhook
    site = web.TCPSite(runner, host=hook.host, port=hook.port)
    await site.start()
    logger.info("HTTP 服务已启动: http://%s:%s%s", hook.host, hook.port, hook.path)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    asyncio.run(run(), loop_factory=_loop_factory())
