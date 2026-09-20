from aiogram import Router

from . import echo, start

routers: tuple[Router, ...] = (start.router, echo.router)
