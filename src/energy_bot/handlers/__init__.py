from aiogram import Router

from . import deposit, echo, rental, start

routers: tuple[Router, ...] = (start.router, rental.router, deposit.router, echo.router)
