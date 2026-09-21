"""租赁状态机单元测试(无数据库)+ 余额订单服务级集成测试(需真实 PG)。"""

import os
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from energy_bot.models import Order, OrderStatus
from energy_bot.repositories import orders as orders_repo
from energy_bot.repositories import users as users_repo
from energy_bot.services import rental, wallet
from energy_bot.services.wallet import WalletError

# 知名黑洞地址(0x41 + 20 个零字节),base58check 校验和有效
VALID_ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
# 0x41 + 字节 1..20 的确定性有效地址
VALID_ADDRESS_2 = "TA4Y62o6YC2Zsck9rZVGTvqW1AQ7X9zTnj"


def _order(status: OrderStatus) -> Order:
    return Order(
        user_id=1,
        recipient_address=VALID_ADDRESS,
        energy_amount=1000,
        duration_minutes=60,
        price=Decimal("1"),
        status=status,
    )


def test_allowed_transitions_pass() -> None:
    for source, targets in rental.ALLOWED_TRANSITIONS.items():
        for target in targets:
            rental.ensure_transition(_order(source), target)  # 不抛即通过


def test_denied_transitions_raise() -> None:
    for source, targets in rental.ALLOWED_TRANSITIONS.items():
        for target in OrderStatus:
            if target in targets or target is source:
                continue
            with pytest.raises(rental.InvalidTransitionError):
                rental.ensure_transition(_order(source), target)


def test_terminal_states_have_no_exits() -> None:
    for terminal in (OrderStatus.ACTIVE, OrderStatus.REFUNDED):
        assert rental.ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_tron_address_format() -> None:
    assert rental.TRON_ADDRESS_RE.fullmatch(VALID_ADDRESS)
    assert not rental.TRON_ADDRESS_RE.fullmatch("0x1234567890")
    assert not rental.TRON_ADDRESS_RE.fullmatch("t" + "2" * 33)  # 必须大写 T
    assert not rental.TRON_ADDRESS_RE.fullmatch("T" + "0IilO" + "2" * 28)  # 非 base58 字符
    assert not rental.TRON_ADDRESS_RE.fullmatch("T" + "2" * 32)  # 长度不足


def test_tron_address_checksum() -> None:
    assert rental.is_valid_tron_address(VALID_ADDRESS)
    assert rental.is_valid_tron_address(VALID_ADDRESS_2)
    # 篡改一位:格式依旧合法,但校验和不匹配
    assert rental.TRON_ADDRESS_RE.fullmatch(VALID_ADDRESS[:-1] + "X")
    assert not rental.is_valid_tron_address(VALID_ADDRESS[:-1] + "X")
    assert not rental.is_valid_tron_address("T" + "2" * 33)  # 过正则但无校验和
    assert not rental.is_valid_tron_address("0x1234567890")
    assert not rental.is_valid_tron_address("")


@pytest.mark.skipif(
    not os.environ.get("ENERGY_BOT_TEST_DSN"),
    reason="需要 ENERGY_BOT_TEST_DSN 指向可用的 PostgreSQL(测试库,数据会被清空)",
)
async def test_wallet_order_lifecycle(db_factory) -> None:
    factory = db_factory

    # 下单:地址校验先于写库,reserved 落库并冻结销售金额
    async with factory() as session:
        user = await users_repo.upsert_user(
            session, user_id=42, first_name="租赁客", language_code="zh"
        )
        await wallet.credit(session, user_id=user.id, amount=Decimal("10"), reference="seed-42")
        with pytest.raises(rental.RentalError, match="TRON"):
            await rental.reserve_order(
                session,
                user_id=user.id,
                request_key="tg:bad",
                recipient_address="0xabc",
                energy_amount=65000,
                duration_minutes=60,
                price=Decimal("1"),
            )
        order = await rental.reserve_order(
            session,
            user_id=user.id,
            request_key="tg:1:1",
            recipient_address=VALID_ADDRESS,
            energy_amount=131072,
            duration_minutes=120,
            price=Decimal("5.5"),
        )
        assert order.status is OrderStatus.RESERVED and order.wallet_state == "held"
        assert order.duration_minutes == 120
        order_id = order.id
        await session.commit()

    # 相同请求号幂等重放;同号不同参数拒绝
    async with factory() as session:
        replay = await rental.reserve_order(
            session,
            user_id=42,
            request_key="tg:1:1",
            recipient_address=VALID_ADDRESS,
            energy_amount=131072,
            duration_minutes=120,
            price=Decimal("5.5"),
        )
        assert replay.id == order_id
        with pytest.raises(rental.RentalError, match="请求标识"):
            await rental.reserve_order(
                session,
                user_id=42,
                request_key="tg:1:1",
                recipient_address=VALID_ADDRESS_2,
                energy_amount=131072,
                duration_minutes=120,
                price=Decimal("5.5"),
            )
        await session.commit()

    # 采购提交 → 能量到账:冻结款转为实扣,ACTIVE 不管理上游租期
    async with factory() as session:
        order = await orders_repo.get_order_for_update(session, order_id)
        assert order is not None
        await rental.begin_purchase(session, order, "tronow")
        assert order.status is OrderStatus.DELEGATING
        await rental.complete_purchase(session, order, cost=Decimal("4.0"), txid="tx-123")
        assert order.status is OrderStatus.ACTIVE and order.wallet_state == "captured"
        assert order.upstream_txid == "tx-123"
        account = await wallet.lock_wallet(session, 42)
        assert account.available == Decimal("4.5") and account.frozen == 0
        await session.commit()

    # ACTIVE 是终态:不能取消
    async with factory() as session:
        with pytest.raises(rental.RentalError, match="采购"):
            await rental.cancel_order(session, user_id=42, order_id=order_id)
        await session.commit()

    # 第二单:取消即解冻退款
    async with factory() as session:
        order2 = await rental.reserve_order(
            session,
            user_id=42,
            request_key="tg:1:2",
            recipient_address=VALID_ADDRESS_2,
            energy_amount=65000,
            duration_minutes=60,
            price=Decimal("2.5"),
        )
        await session.commit()
    async with factory() as session:
        order2 = await rental.cancel_order(session, user_id=42, order_id=order2.id)
        assert order2.status is OrderStatus.REFUNDED and order2.wallet_state == "released"
        account = await wallet.lock_wallet(session, 42)
        assert account.available == Decimal("4.5") and account.frozen == 0
        await session.commit()

    # 按用户查询
    async with factory() as session:
        mine = await orders_repo.list_by_user(session, 42)
        assert [o.id for o in mine] == [order2.id, order_id]  # 按创建时间倒序


async def test_upstream_identity_is_scoped_and_unique(db_factory) -> None:
    """(provider, upstream_order_id) 唯一约束:跨供应商可同号,同供应商冲突。"""
    async with db_factory() as session:
        await users_repo.upsert_user(
            session, user_id=1, first_name="唯一性测试", language_code="zh"
        )
        for provider in ("tronow", "tronbid"):
            session.add(
                Order(
                    user_id=1,
                    recipient_address=VALID_ADDRESS,
                    energy_amount=65000,
                    duration_minutes=60,
                    price=Decimal("1"),
                    status=OrderStatus.DELEGATING,
                    provider=provider,
                    upstream_order_id="same-id",
                )
            )
        await session.flush()
        await session.commit()
        session.add(
            Order(
                user_id=1,
                recipient_address=VALID_ADDRESS,
                energy_amount=65000,
                duration_minutes=60,
                price=Decimal("1"),
                status=OrderStatus.DELEGATING,
                provider="tronow",
                upstream_order_id="same-id",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
        rows = (
            await session.scalars(select(Order).where(Order.upstream_order_id == "same-id"))
        ).all()
        assert {row.provider for row in rows} == {"tronow", "tronbid"}


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
async def test_nonfinite_price_rejected_before_database(price: Decimal) -> None:
    with pytest.raises(WalletError, match="金额"):
        await rental.reserve_order(
            AsyncMock(),
            user_id=1,
            request_key="tg:x",
            recipient_address=VALID_ADDRESS,
            energy_amount=65000,
            duration_minutes=60,
            price=price,
        )
