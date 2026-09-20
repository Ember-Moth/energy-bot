from aiogram import Router

from . import echo, rental, start

routers: tuple[Router, ...] = (start.router, rental.router, echo.router)
