"""租赁状态机单元测试(无数据库)+ 完整生命周期集成测试(需真实 PG)。"""

import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from energy_bot.models import Order, OrderStatus
from energy_bot.repositories import orders as orders_repo
from energy_bot.repositories import users as users_repo
from energy_bot.services import rental

# 知名黑洞地址(0x41 + 20 个零字节),base58check 校验和有效
VALID_ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
# 0x41 + 字节 1..20 的确定性有效地址
VALID_ADDRESS_2 = "TA4Y62o6YC2Zsck9rZVGTvqW1AQ7X9zTnj"


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
    for terminal in (OrderStatus.EXPIRED, OrderStatus.REFUNDED):
        assert rental.ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_failed_order_can_be_refunded() -> None:
    # 上游执行失败后的售后路径:failed → refunded
    rental.ensure_transition(_order(OrderStatus.FAILED), OrderStatus.REFUNDED)


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
async def test_rental_lifecycle(db_factory) -> None:
    factory = db_factory

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

        order = await rental.activate(
            session, order_id, upstream_txid="tx-123", confirmed_at=rental._now()
        )
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

    # 第二单走售后路径:委托上游 → 执行失败 → 退款
    async with factory() as session:
        failed_order = await rental.place_order(
            session,
            user_id=user.id,
            recipient_address=VALID_ADDRESS_2,
            energy_amount=65000,
            duration_hours=1,
            price=Decimal("2.5"),
        )
        failed_id = failed_order.id
        await session.commit()
    async with factory() as session:
        await rental.mark_paid(session, failed_id)
        await rental.start_delegation(
            session, failed_id, provider="upstream-a", upstream_order_id="UP-2"
        )
        order = await rental.fail(session, failed_id)
        assert order.status is OrderStatus.FAILED
        order = await rental.refund(session, failed_id)  # failed → refunded 售后通道
        assert order.status is OrderStatus.REFUNDED
        await session.commit()

    # 对账与按用户查询
    async with factory() as session:
        by_upstream = await orders_repo.get_by_upstream_order_id(
            session, "UP-1", provider="upstream-a"
        )
        assert by_upstream is not None and by_upstream.id == order_id
        mine = await orders_repo.list_by_user(session, 42)
        assert [o.id for o in mine] == [failed_id, order_id]  # 按创建时间倒序


@pytest.mark.parametrize("expired", [False, True])
async def test_delayed_activation_uses_upstream_expiry(db_factory, expired: bool) -> None:
    async with db_factory() as session:
        await users_repo.upsert_user(session, user_id=1, first_name="租期测试", language_code="zh")
        order = await rental.place_order(
            session,
            user_id=1,
            recipient_address=VALID_ADDRESS,
            energy_amount=65000,
            duration_hours=1,
            price=Decimal("1"),
        )
        await rental.mark_paid(session, order.id)
        await rental.start_delegation(
            session, order.id, provider="tronow", upstream_order_id="ord_time"
        )
        with pytest.raises(rental.RentalError, match="租期时间"):
            await rental.activate(session, order.id)
        assert order.status is OrderStatus.DELEGATING
        expiry = rental._now() + timedelta(minutes=-20 if expired else 20)
        await rental.activate(session, order.id, lease_expires_at=expiry)
        await session.commit()
        assert order.expires_at == expiry
        assert order.status is (OrderStatus.EXPIRED if expired else OrderStatus.ACTIVE)


async def test_upstream_identity_is_scoped_and_unique(db_factory) -> None:
    async with db_factory() as session:
        await users_repo.upsert_user(
            session, user_id=1, first_name="唯一性测试", language_code="zh"
        )
        identifiers = []
        for provider in ("tronow", "tronbid", "tronow"):
            order = await rental.place_order(
                session,
                user_id=1,
                recipient_address=VALID_ADDRESS,
                energy_amount=65000,
                duration_hours=1,
                price=Decimal("1"),
            )
            await rental.mark_paid(session, order.id)
            identifiers.append(order.id)
            if len(identifiers) < 3:
                await rental.start_delegation(
                    session, order.id, provider=provider, upstream_order_id="same-id"
                )
        await session.commit()
        for provider, order_id in zip(("tronow", "tronbid"), identifiers[:2], strict=True):
            found = await rental.get_by_upstream(
                session, provider=provider, upstream_order_id="same-id"
            )
            assert found is not None and found.id == order_id
        with pytest.raises(IntegrityError):
            await rental.start_delegation(
                session, identifiers[2], provider="tronow", upstream_order_id="same-id"
            )
        await session.rollback()


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
async def test_nonfinite_price_rejected_before_database(price: Decimal) -> None:
    with pytest.raises(rental.RentalError, match="price"):
        await rental.place_order(
            AsyncMock(),
            user_id=1,
            recipient_address=VALID_ADDRESS,
            energy_amount=65000,
            duration_hours=1,
            price=price,
        )
