"""订单入口使用配置价、隔离用户订单,资金错误不会提交半成品。"""

from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from energy_bot.config import RentalProduct, RentalSettings, Settings
from energy_bot.handlers.rental import order_detail, rent
from energy_bot.models import Order, Wallet
from energy_bot.repositories.users import upsert_user
from energy_bot.services import wallet

ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


def message(user_id=1, chat_type="private") -> Message:
    msg = AsyncMock()
    msg.from_user = SimpleNamespace(id=user_id, full_name="测试", language_code="zh")
    msg.chat = SimpleNamespace(id=user_id, type=chat_type)
    msg.message_id = 1
    return cast(Message, msg)


def settings() -> RentalSettings:
    return RentalSettings(
        enabled=True,
        products=[
            RentalProduct(
                energy_amount=65000,
                duration_minutes=60,
                price_trx=Decimal("4"),
            )
        ],
    )


async def test_insufficient_balance_does_not_commit_draft(db_factory):
    msg = message()
    async with db_factory() as session:
        await rent(msg, CommandObject(command="rent", args=f"65000 {ADDRESS}"), session, settings())
        await session.commit()
    assert "余额不足" in cast(AsyncMock, msg.answer).call_args.args[0]
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0


async def test_handler_reserves_configured_price_and_replay_is_idempotent(db_factory):
    async with db_factory() as session, session.begin():
        await upsert_user(session, user_id=1, first_name="测试", language_code="zh")
        await wallet.credit(session, user_id=1, amount=Decimal("10"), reference="synthetic")
    for _ in range(2):
        async with db_factory() as session:
            await rent(
                message(),
                CommandObject(command="rent", args=f"65000 {ADDRESS}"),
                session,
                settings(),
            )
            await session.commit()
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        order = await session.scalar(select(Order))
        assert order.price == Decimal("4")
        account = await session.get(Wallet, 1)
        assert account.available == Decimal("6") and account.frozen == Decimal("4")
        other = message(user_id=2)
        await order_detail(other, CommandObject(command="order", args=str(order.id)), session)
        cast(AsyncMock, other.answer).assert_awaited_once_with("订单不存在。")


async def test_group_cannot_place_private_wallet_order():
    session = AsyncMock()
    msg = message(chat_type="group")
    await rent(msg, CommandObject(command="rent", args=f"65000 {ADDRESS}"), session, settings())
    session.execute.assert_not_called()
    cast(AsyncMock, msg.answer).assert_awaited_once_with("请私聊机器人办理租赁。")


async def test_duration_comes_from_product_not_user(db_factory):
    """用户不传租期:订单租期取套餐的 duration_minutes;未上架的能量数量被拒绝。"""
    async with db_factory() as session, session.begin():
        await upsert_user(session, user_id=1, first_name="测试", language_code="zh")
        await wallet.credit(session, user_id=1, amount=Decimal("10"), reference="synthetic")
    async with db_factory() as session:
        msg = message()
        await rent(msg, CommandObject(command="rent", args=f"65000 {ADDRESS}"), session, settings())
        await session.commit()
        order = await session.scalar(select(Order))
        assert order is not None
        assert order.duration_minutes == 60  # 套餐租期,不是用户输入
        assert "60 分钟" in cast(AsyncMock, msg.answer).call_args.args[0]
        # 未上架的能量数量拒绝
        other = message()
        await rent(
            other, CommandObject(command="rent", args=f"131000 {ADDRESS}"), session, settings()
        )
        assert "未上架" in cast(AsyncMock, other.answer).call_args.args[0]


@pytest.mark.parametrize(
    "products",
    [
        [{"energy_amount": 65000, "duration_minutes": 60, "price_trx": "NaN"}],
        [{"energy_amount": 65000, "duration_minutes": 60, "price_trx": "1.0000001"}],
        [{"energy_amount": 65000, "duration_minutes": 0, "price_trx": "1"}],
        [{"energy_amount": 65000, "duration_minutes": 60, "price_trx": "1"}] * 2,
        # 同一能量数量上架两个租期:用户无法区分,配置层直接拒绝
        [
            {"energy_amount": 65000, "duration_minutes": 60, "price_trx": "1"},
            {"energy_amount": 65000, "duration_minutes": 15, "price_trx": "2"},
        ],
    ],
)
def test_invalid_catalog_rejected(products):
    with pytest.raises(ValueError):
        RentalSettings(products=products)


def test_rental_environment_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENERGY_BOT_RENTAL__ENABLED", "true")
    monkeypatch.setenv("ENERGY_BOT_RENTAL__POLL_SECONDS", "10")
    config = Settings()
    assert config.rental.enabled and config.rental.poll_seconds == 10
    assert RentalSettings().enabled is False
