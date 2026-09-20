"""真实 PostgreSQL 验证并发队列、共享刷新、隔离和失效屏障。"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import column, func, select, table, update

from energy_bot.config import TIMEZONE, RentalSettings, Settings, TronowSettings, UpstreamSettings
from energy_bot.db import create_engine_from_dsn, create_session_factory
from energy_bot.models import Order, UpstreamCache, WalletEntry
from energy_bot.services import rental
from energy_bot.services.cached_upstream import CachedTronowClient
from energy_bot.services.procurement import OrderWorker
from energy_bot.services.providers import TronowProvider, build_providers
from energy_bot.services.upstream.tronow import TronowApiError
from energy_bot.services.upstream_cache import CachedValue, PostgresCache, cache_key
from energy_bot.services.upstream_gate import UpstreamDeferred, UpstreamGate
from test_order_system import FakeProvider, funded, reserve


def value(number=1):
    return CachedValue({"cost": str(number)}, datetime.now(TIMEZONE) + timedelta(seconds=10))


async def test_unlogged_only_for_cache(db_factory):
    catalog = table("pg_class", column("relname"), column("relpersistence"))
    async with db_factory() as session:
        rows = dict(
            (
                await session.execute(
                    select(catalog.c.relname, catalog.c.relpersistence).where(
                        catalog.c.relname.in_(
                            (
                                "upstream_cache",
                                "orders",
                                "wallet_entries",
                                "order_notifications",
                                "upstream_throttles",
                            )
                        )
                    )
                )
            ).all()
        )
    rows = {key: val.decode() if isinstance(val, bytes) else val for key, val in rows.items()}
    assert rows.pop("upstream_cache") == "u"
    assert set(rows.values()) == {"p"}
    assert len(rows) == 4


async def test_parallel_consumers_no_duplicate_settlement(db_factory):
    await funded(db_factory, amount="100")
    for i in range(12):
        await reserve(db_factory, key=str(i))
    provider = FakeProvider()
    original = provider.submit
    active = maximum = 0
    barrier = asyncio.Event()

    async def slow(attempt):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        if active == 4:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 5)
        result = await original(attempt)
        active -= 1
        return result

    provider.submit = slow
    settings = RentalSettings(order_concurrency=4, batch_size=12)
    a = OrderWorker(db_factory, {provider.name: provider}, settings)
    b = OrderWorker(db_factory, {provider.name: provider}, settings)
    await asyncio.gather(a.tick(), b.tick())
    assert 4 <= maximum <= 8
    assert len(provider.posts) == len(provider.charges) == 12
    async with db_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(WalletEntry)
                .where(WalletEntry.key.startswith("capture:"))
            )
            == 12
        )
        assert await session.scalar(select(func.sum(WalletEntry.available_delta))) == Decimal("52")


async def test_notification_not_blocked_by_slow_order(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    async with db_factory() as session, session.begin():
        order = await session.get(Order, order_id)
        await rental.notify_order(session, order, "test", "独立通知")
    provider = FakeProvider()
    provider.quote_started, provider.quote_continue = asyncio.Event(), asyncio.Event()
    delivered = asyncio.Event()

    async def send(user_id, text):
        delivered.set()

    service = OrderWorker(
        db_factory,
        {provider.name: provider, "tronbid": FakeProvider("tronbid", price="3")},
        RentalSettings(order_concurrency=2, idle_poll_seconds=0.05),
        send,
    )
    service.start()
    try:
        await asyncio.wait_for(provider.quote_started.wait(), 5)
        await asyncio.wait_for(delivered.wait(), 5)
        assert not provider.quote_continue.is_set()
    finally:
        await service.close()


async def test_notification_order_preserved_with_parallel_delivery(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    async with db_factory() as session, session.begin():
        order = await session.get(Order, order_id)
        await rental.notify_order(session, order, "first", "first")
        await rental.notify_order(session, order, "second", "second")
    sent = []

    async def send(user_id, text):
        if text == "first":
            await asyncio.sleep(0.05)
        sent.append(text)

    service = OrderWorker(db_factory, {}, RentalSettings(notification_concurrency=4), send)
    await service.deliver_notifications()
    assert sent == ["first", "second"]


async def test_cache_single_refresh_across_instances(db_factory):
    a, b = PostgresCache(db_factory), PostgresCache(db_factory)
    load = AsyncMock(return_value=value())
    results = await asyncio.gather(*(c.get("shared", load) for c in [a, b] * 20))
    assert load.await_count == 1
    assert len(results) == 40
    async with db_factory() as session, session.begin():
        await session.execute(
            update(UpstreamCache).values(expires_at=datetime.now(TIMEZONE) - timedelta(seconds=1))
        )
    await b.get("shared", load)
    assert load.await_count == 2
    await a.close()
    await b.close()


async def test_invalidation_fences_inflight_refresh(db_factory):
    cache = PostgresCache(db_factory)
    started, resume = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await resume.wait()
        return value(calls)

    task = asyncio.create_task(cache.get("balance", load))
    await asyncio.wait_for(started.wait(), 5)
    await cache.invalidate("balance")
    resume.set()
    assert (await task).payload == {"cost": "2"}
    async with db_factory() as session:
        assert (await session.get(UpstreamCache, "balance")).payload == {"cost": "2"}
    await cache.close()


async def test_cancel_one_waiter_does_not_cancel_shared_refresh(db_factory):
    cache = PostgresCache(db_factory)
    started, resume = asyncio.Event(), asyncio.Event()

    async def load():
        started.set()
        await resume.wait()
        return value()

    first = asyncio.create_task(cache.get("shared", load))
    await asyncio.wait_for(started.wait(), 5)
    second = asyncio.create_task(cache.get("shared", load))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    resume.set()
    assert (await second).payload == {"cost": "1"}
    await cache.close()


async def test_cache_loader_failure_can_retry(db_factory):
    cache = PostgresCache(db_factory)
    load = AsyncMock(side_effect=[TimeoutError(), value()])
    with pytest.raises(TimeoutError):
        await cache.get("recover", load)
    assert (await cache.get("recover", load)).payload == {"cost": "1"}
    await cache.close()


async def test_expired_refresh_lease_is_recoverable(db_factory):
    async with db_factory() as session, session.begin():
        session.add(
            UpstreamCache(
                key="orphan",
                refresh_token="dead-process",
                refresh_until=datetime.now(TIMEZONE) - timedelta(seconds=1),
            )
        )
    cache = PostgresCache(db_factory)
    assert (await cache.get("orphan", AsyncMock(return_value=value()))).payload == {"cost": "1"}
    await cache.close()


def test_cache_namespace_separates_credentials_and_products():
    assert (
        len(
            {
                cache_key("url", credential, minutes)
                for credential in ("key-a", "key-b")
                for minutes in (15, 60)
            }
        )
        == 4
    )


async def test_global_request_budget_shared_across_instances(db_factory):
    settings = RentalSettings(upstream_requests_per_second=20, upstream_orders_per_second=2)
    a, b = (
        UpstreamGate(db_factory, "merchant", settings),
        UpstreamGate(db_factory, "merchant", settings),
    )
    assert await a._delay(True) == 0
    delay = await b._delay(True)
    assert 0 < delay <= 0.5
    # 其他商户的额度不会被占用。
    assert await UpstreamGate(db_factory, "other", settings)._delay(True) == 0


async def test_retry_after_blocks_other_process(db_factory):
    a = UpstreamGate(db_factory, "merchant", RentalSettings())
    b = UpstreamGate(db_factory, "merchant", RentalSettings())
    request = AsyncMock(side_effect=TronowApiError("RATE_LIMITED", 429, retry_after=120))
    with pytest.raises(TronowApiError):
        await a.run(request, creation=True)
    untouched = AsyncMock()
    with pytest.raises(UpstreamDeferred) as exc:
        await b.run(untouched, creation=True)
    assert exc.value.retry_after >= 119
    untouched.assert_not_called()


async def test_cached_client_ttl_and_balance_invalidation(db_factory):
    calls = {"quote": 0, "balance": 0, "post": 0}

    async def endpoint(request):
        if request.path.endswith("quote"):
            calls["quote"] += 1
            data = {
                "resource_amount": 65000,
                "duration": "1h",
                "price_sun": "2000000",
                "currency": "TRX",
                "priced_at": datetime.now(TIMEZONE).isoformat(),
            }
        elif request.path.endswith("balance"):
            calls["balance"] += 1
            data = {
                "currency": "TRX",
                "available_balance_sun": "10000000",
                "reserved_balance_sun": "0",
                "total_balance_sun": "10000000",
                "updated_at": datetime.now(TIMEZONE).isoformat(),
            }
        else:
            calls["post"] += 1
            data = {
                "order_id": "ord_test",
                "client_order_id": "eb-cache",
                "status": "PROCESSING",
                "reserved_amount_sun": "2000000",
                "currency": "TRX",
                "created_at": datetime.now(TIMEZONE).isoformat(),
            }
        return web.json_response({"code": "OK", "data": data})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", endpoint)
    async with TestClient(TestServer(app)) as server:
        settings = RentalSettings(
            upstream_requests_per_second=1000, upstream_orders_per_second=1000
        )
        cache = PostgresCache(db_factory)
        client = CachedTronowClient(
            TronowSettings(base_url=str(server.make_url("/openapi/v1"))),
            cache,
            settings,
            UpstreamGate(db_factory, "mock", settings),
        )
        try:
            quotes = await asyncio.gather(*(client.get_quote(65000) for _ in range(20)))
            assert calls["quote"] == 1
            assert len({quote.valid_until for quote in quotes}) == 1
            assert (await client.get_quote(65000)).valid_until == quotes[0].valid_until
            await asyncio.gather(*(client.get_balance() for _ in range(20)))
            assert calls["balance"] == 1
            await client.submit_order(b'{"client_order_id":"eb-cache"}', idempotency_key="eb-cache")
            await client.get_balance()
            assert calls == {"quote": 1, "balance": 2, "post": 1}
        finally:
            await client.close()


async def test_cache_refresh_does_not_hold_database_connection(db_factory):
    engine = create_engine_from_dsn(
        db_factory.kw["bind"].url.render_as_string(hide_password=False), pool_size=1, max_overflow=0
    )
    cache = PostgresCache(create_session_factory(engine))
    started, resume = asyncio.Event(), asyncio.Event()

    async def load():
        started.set()
        await resume.wait()
        return value()

    task = asyncio.create_task(cache.get("one-connection", load))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with asyncio.timeout(1), cache.factory() as session:
            assert await session.scalar(select(1)) == 1
        resume.set()
        await task
    finally:
        await cache.close()
        await engine.dispose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("order_concurrency", 0),
        ("notification_concurrency", 0),
        ("idle_poll_seconds", 0),
        ("quote_cache_seconds", 11),
        ("balance_cache_seconds", 6),
        ("upstream_concurrency", 0),
        ("upstream_requests_per_second", 0),
        ("upstream_orders_per_second", 0),
    ],
)
def test_concurrency_config_validation(field, value):
    with pytest.raises(ValueError):
        RentalSettings(**{field: value})


def test_concurrency_env_override(monkeypatch):
    monkeypatch.setenv("ENERGY_BOT_RENTAL__ORDER_CONCURRENCY", "16")
    monkeypatch.setenv("ENERGY_BOT_RENTAL__QUOTE_CACHE_SECONDS", "0")
    monkeypatch.setenv("ENERGY_BOT_UPSTREAM__TRONOW__ACCOUNT_SCOPE", "merchant-a")
    config = Settings()
    assert config.rental.order_concurrency == 16
    assert config.rental.quote_cache_seconds == 0
    assert config.upstream.tronow.account_scope == "merchant-a"


async def test_merchant_limit_scope_is_separate_from_cache_credentials(db_factory):
    configs = [
        UpstreamSettings(
            tronow=TronowSettings(
                api_key=key, api_secret="synthetic", account_scope="same-merchant"
            )
        )
        for key in ("key-a", "key-b")
    ]
    groups = [build_providers(config, db_factory, RentalSettings()) for config in configs]
    a, b = (cast(TronowProvider, group["tronow"]).client for group in groups)
    try:
        assert a._gate is not None and b._gate is not None
        assert a._gate.scope == b._gate.scope
        assert cast(CachedTronowClient, a).scope != cast(CachedTronowClient, b).scope
    finally:
        await a.close()
        await b.close()
