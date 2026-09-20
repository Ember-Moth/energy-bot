import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user_id = data["event_from_user"].id if "event_from_user" in data else None
        if isinstance(event, Message):
            logger.info("Message from %s: %s", user_id, event.text or event.content_type)
        elif isinstance(event, CallbackQuery):
            logger.info("Callback from %s: %s", user_id, event.data)
        return await handler(event, data)
