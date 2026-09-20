"""单上游直采与多上游询价的边界,以及无报价订单的资金恢复。"""

from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from energy_bot.models import Order, OrderStatus, PurchaseAttempt
from energy_bot.services.providers import Product, ProviderMismatch, TronowProvider
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient
from test_order_system import ADDRESS, FakeProvider, assert_money, due, funded, reserve, worker


@pytest.mark.parametrize("name,minutes", [("tronow", 60), ("tronbid", 15)])
async def test_single_provider_skips_quote_and_records_unknown_quote(db_factory, name, minutes):
    await funded(db_factory)
    order_id = await reserve(db_factory, minutes=minutes)
    provider = FakeProvider(name, price="8")  # 高于销售价及预算也不伪造预报价拦截
    provider.quote = AsyncMock(side_effect=AssertionError("单上游不应询价"))
    await worker(db_factory, provider).tick()
    provider.quote.assert_not_awaited()
    assert len(provider.posts) == 1
    async with db_factory() as session:
        attempt = await session.scalar(select(PurchaseAttempt))
        order = await session.get(Order, order_id)
        assert attempt.quoted_cost is None and attempt.actual_cost == Decimal("8")
        assert order.purchase_cost == Decimal("8") and order.last_error == "COST_OVERRUN"
    await assert_money(db_factory, "6", "0", captures=1)


async def test_single_tronow_unsupported_duration_never_calls_upstream(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory, minutes=15)
    client = AsyncMock(spec=TronowClient)
    provider = TronowProvider(client)
    await worker(db_factory, provider).tick()
    client.get_quote.assert_not_awaited()
    client.get_balance.assert_not_awaited()
    client.submit_order.assert_not_awaited()
    async with db_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status is OrderStatus.REFUNDED and order.last_error == "UNSUPPORTED_PRODUCT"
        assert await session.scalar(select(func.count()).select_from(PurchaseAttempt)) == 0
    with pytest.raises(ProviderMismatch):
        provider.body(Product(ADDRESS, 65000, 15), "business-id")
    await assert_money(db_factory, "10", "0", releases=1)


@pytest.mark.parametrize("rejected", [False, True])
async def test_single_failure_releases_without_requote_or_repurchase(db_factory, rejected):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider(state="failed")
    provider.quote = AsyncMock(side_effect=AssertionError("不应询价"))
    if rejected:
        provider.submit = AsyncMock(side_effect=TronowApiError("INSUFFICIENT_BALANCE", 422))
    await worker(db_factory, provider).tick()
    await due(db_factory, order_id)
    await worker(db_factory, provider).tick()
    await worker(db_factory, provider).tick()
    provider.quote.assert_not_awaited()
    if rejected:
        cast(AsyncMock, provider.submit).assert_awaited_once()
    else:
        assert len(provider.posts) == 1
    await assert_money(db_factory, "10", "0", releases=1)


async def test_recovery_does_not_reselect_when_configuration_grows(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    original, other = FakeProvider(), FakeProvider("tronbid")
    original.quote = AsyncMock(side_effect=AssertionError("直采及恢复不应询价"))
    other.quote = AsyncMock(side_effect=AssertionError("原单恢复不应询价"))
    original.error = TimeoutError()
    await worker(db_factory, original).tick()
    async with db_factory() as session:
        before = await session.scalar(select(PurchaseAttempt))
        identity = (before.id, before.business_id, before.idempotency_key, before.request_body)
    await due(db_factory, order_id)
    await worker(db_factory, original, other).tick()
    original.quote.assert_not_awaited()
    other.quote.assert_not_awaited()
    assert len(original.posts) == 1 and not other.posts
    async with db_factory() as session:
        after = await session.scalar(select(PurchaseAttempt))
        assert (after.id, after.business_id, after.idempotency_key, after.request_body) == identity
        assert after.quoted_cost is None
    await assert_money(db_factory, "6", "0", captures=1)


async def test_multiple_configured_with_one_available_still_quotes(db_factory):
    await funded(db_factory)
    await reserve(db_factory)
    unavailable, available = FakeProvider(), FakeProvider("tronbid", price="3")
    unavailable.quote = AsyncMock(side_effect=TimeoutError())
    available.quote = AsyncMock(wraps=available.quote)
    await worker(db_factory, unavailable, available).tick()
    unavailable.quote.assert_awaited_once()
    available.quote.assert_awaited_once()
    assert not unavailable.posts and len(available.posts) == 1
    async with db_factory() as session:
        attempt = await session.scalar(select(PurchaseAttempt))
        assert attempt.provider == "tronbid" and attempt.quoted_cost == Decimal("3")
    await assert_money(db_factory, "6", "0", captures=1)


async def test_multi_provider_fallback_keeps_quote_step(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    first, fallback = FakeProvider(state="failed"), FakeProvider("tronbid", price="3")
    fallback.quote = AsyncMock(wraps=fallback.quote)
    service = worker(db_factory, first, fallback)
    await service.tick()
    await due(db_factory, order_id)
    await service.tick()
    assert fallback.quote.await_count == 2
    async with db_factory() as session:
        attempts = list(
            (
                await session.scalars(select(PurchaseAttempt).order_by(PurchaseAttempt.sequence))
            ).all()
        )
        assert [a.quoted_cost for a in attempts] == [Decimal("2"), Decimal("3")]
    await assert_money(db_factory, "6", "0", captures=1)
