import asyncio
import logging
import secrets
import signal
from contextlib import AsyncExitStack
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import setup_application
from aiohttp import web

from energy_bot.config import load_settings
from energy_bot.db import create_engine_from_dsn, create_session_factory
from energy_bot.handlers import routers
from energy_bot.logging_config import setup_logging
from energy_bot.middlewares.db import DbSessionMiddleware
from energy_bot.middlewares.logging import LoggingMiddleware
from energy_bot.web.health import register_health_routes
from energy_bot.web.telegram import register_telegram_routes
from energy_bot.web.tronow import register_tronow_webhook

logger = logging.getLogger(__name__)


async def amain(config_path: Path | None = None) -> None:
    """异步主体:加载配置、初始化日志、装配 webhook 服务并优雅挂起。"""
    settings = load_settings(config_path)
    setup_logging(
        level=settings.logging.level,
        log_dir=settings.logging.log_dir or None,
        json_logs=settings.logging.json_logs,
    )
    logger.info("事件循环: %s", type(asyncio.get_running_loop()).__module__)
    if not settings.bot_token:
        raise SystemExit(
            "bot_token 未配置:请在 config.yaml 填入 @BotFather 的 token,或设置 ENERGY_BOT_BOT_TOKEN"
        )
    if not settings.webhook.base_url:
        raise SystemExit("webhook.base_url 未配置:请填入公网 HTTPS 地址,如 https://bot.example.com")

    try:
        dsn = settings.database.effective_dsn()
    except ValueError as exc:
        raise SystemExit(f"database 配置不完整:{exc}") from exc
    engine = create_engine_from_dsn(
        dsn,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
    )
    session_factory = create_session_factory(engine)

    hook = settings.webhook
    secret_token = hook.secret_token or secrets.token_urlsafe(32)
    if not hook.secret_token:
        logger.info("webhook.secret_token 未配置,已自动生成(仅本次启动有效)")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(DbSessionMiddleware(session_factory))
    dp.callback_query.middleware(LoggingMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware(session_factory))
    dp.include_routers(*routers)

    stop = asyncio.Event()
    async with AsyncExitStack() as resources:
        resources.push_async_callback(bot.session.close)
        resources.push_async_callback(engine.dispose)

        app = web.Application()
        register_telegram_routes(app, dp, bot, hook.path, secret_token)
        register_health_routes(app)
        register_tronow_webhook(app, settings.upstream.tronow, session_factory)
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app, access_log=None)  # 访问日志交给 LoggingMiddleware,避免刷屏
        resources.push_async_callback(runner.cleanup)
        await runner.setup()
        await web.TCPSite(runner, host=hook.host, port=hook.port).start()

        webhook_url = f"{hook.base_url}{hook.path}"
        await bot.set_webhook(
            webhook_url,
            secret_token=secret_token,
            drop_pending_updates=False,
        )
        resources.push_async_callback(bot.delete_webhook)
        logger.info("webhook 已注册: %s", webhook_url)
        logger.info("监听 %s:%d%s", hook.host, hook.port, hook.path)

        # SIGTERM(systemd stop)→ 触发优雅停机;退出栈按 LIFO 清理:
        # delete_webhook → aiohttp 下线(dp.shutdown)→ bot 会话关闭
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        resources.callback(loop.remove_signal_handler, signal.SIGTERM)

        await stop.wait()
    logger.info("已优雅停机")
