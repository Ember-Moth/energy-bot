"""Telegram 不得在事务 handler 完成前确认更新。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from energy_bot.web.telegram import register_telegram_routes


async def test_update_waits_for_handler_and_errors_are_retryable():
    started, release = asyncio.Event(), asyncio.Event()

    async def process(*args, **kwargs):
        started.set()
        await release.wait()

    dp = MagicMock(spec=Dispatcher)
    dp.feed_raw_update = AsyncMock(side_effect=process)
    bot = Bot("123456:" + "x" * 35)
    app = web.Application()
    register_telegram_routes(app, dp, bot, "/webhook", "synthetic")
    async with TestClient(TestServer(app)) as http:
        task = asyncio.create_task(
            http.post(
                "/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic"},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not task.done()
        release.set()
        assert (await task).status == 200
        dp.feed_webhook_update.assert_not_called()
        dp.feed_raw_update.side_effect = RuntimeError("synthetic transaction failure")
        response = await http.post(
            "/webhook",
            json={"update_id": 2},
            headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic"},
        )
        assert response.status == 500
