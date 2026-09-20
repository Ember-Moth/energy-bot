"""租赁状态机单元测试(无数据库)+ 完整生命周期集成测试(需真实 PG)。"""

import os
from datetime import timedelta
from decimal import Decimal

import pytest

from energy_bot.db import create_engine_from_dsn, create_session_factory
from energy_bot.models import Base, Order, OrderStatus
from energy_bot.repositories import orders as orders_repo
from energy_bot.repositories import users as users_repo
from energy_bot.services import rental

VALID_ADDRESS = "T" + "2" * 33


def _order(status: OrderStatus) -> Order:
    return Order(
        user_id=1,
        recipient_address=VALID_ADDRESS,
        energy_amount=1000,
        duration_hours=1,
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
    for terminal in (OrderStatus.EXPIRED, OrderStatus.FAILED, OrderStatus.REFUNDED):
        assert rental.ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_tron_address_format() -> None:
    assert rental.TRON_ADDRESS_RE.fullmatch(VALID_ADDRESS)
    assert not rental.TRON_ADDRESS_RE.fullmatch("0x1234567890")
    assert not rental.TRON_ADDRESS_RE.fullmatch("t" + "2" * 33)  # 必须大写 T
    assert not rental.TRON_ADDRESS_RE.fullmatch("T" + "0IilO" + "2" * 28)  # 非 base58 字符
    assert not rental.TRON_ADDRESS_RE.fullmatch("T" + "2" * 32)  # 长度不足


@pytest.mark.skipif(
    not os.environ.get("ENERGY_BOT_TEST_DSN"),
    reason="需要 ENERGY_BOT_TEST_DSN 指向可用的 PostgreSQL(测试库,数据会被清空)",
)
async def test_rental_lifecycle() -> None:
    engine = create_engine_from_dsn(os.environ["ENERGY_BOT_TEST_DSN"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    # 下单:地址/参数校验 + draft 落库
    async with factory() as session:
        user = await users_repo.upsert_user(
            session, user_id=42, first_name="租赁客", language_code="zh"
        )
        with pytest.raises(rental.RentalError, match="TRON"):
            await rental.place_order(
                session,
                user_id=user.id,
                recipient_address="0xabc",
                energy_amount=1000,
                duration_hours=1,
                price=Decimal("1"),
            )
        order = await rental.place_order(
            session,
            user_id=user.id,
            recipient_address=VALID_ADDRESS,
            energy_amount=131072,
            duration_hours=2,
            price=Decimal("5.5"),
        )
        assert order.status is OrderStatus.DRAFT
        order_id = order.id
        await session.commit()

    # 收款 → 委托上游 → 能量到账
    async with factory() as session:
        order = await rental.mark_paid(session, order_id)
        assert order.status is OrderStatus.PAID

        order = await rental.start_delegation(
            session, order_id, provider="upstream-a", upstream_order_id="UP-1"
        )
        assert order.status is OrderStatus.DELEGATING
        assert order.delegated_at is not None

        order = await rental.activate(session, order_id, upstream_txid="tx-123")
        assert order.status is OrderStatus.ACTIVE
        assert order.upstream_txid == "tx-123"
        assert order.expires_at is not None
        assert order.delegated_at is not None
        # 租期从到账时刻起算(激活晚于委托,故差值 ≥ 2 小时)
        assert order.expires_at - order.delegated_at >= timedelta(hours=2)
        await session.commit()

    # ACTIVE 不能直接退款;到期回收转 expired
    async with factory() as session:
        with pytest.raises(rental.InvalidTransitionError):
            await rental.refund(session, order_id)
        order = await rental.expire(session, order_id)
        assert order.status is OrderStatus.EXPIRED
        await session.commit()

    # 对账与按用户查询
    async with factory() as session:
        by_upstream = await orders_repo.get_by_upstream_order_id(session, "UP-1")
        assert by_upstream is not None and by_upstream.id == order_id
        mine = await orders_repo.list_by_user(session, 42)
        assert [o.id for o in mine] == [order_id]
    await engine.dispose()
