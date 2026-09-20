"""真实 PostgreSQL 下的订单资金、采购恢复与并发不变量。"""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from energy_bot.config import TIMEZONE, RentalSettings, TronbidSettings, TronowSettings
from energy_bot.models import (
    Order,
    OrderNotification,
    OrderStatus,
    PurchaseAttempt,
    Wallet,
    WalletEntry,
)
from energy_bot.repositories.users import upsert_user
from energy_bot.services import rental, wallet
from energy_bot.services.procurement import OrderWorker, wake_tronow
from energy_bot.services.providers import (
    Offer,
    Product,
    PurchaseResult,
    TronbidProvider,
    TronowProvider,
)
from energy_bot.services.upstream.tronbid import TronbidClient
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient

ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


class FakeProvider:
    def __init__(self, name="tronow", price="2", state="success"):
        self.name = name
        self.price = Decimal(price)
        self.state = state
        self.posts = []
        self.queries = []
        self.charges = {}
        self.error: BaseException | None = None
        self.quote_error = False
        self.quote_started: asyncio.Event | None = None
        self.quote_continue: asyncio.Event | None = None
        self.actual_cost: Decimal | None = None

    def supports(self, product: Product) -> bool:
        return self.name != "tronow" or product.minutes == 60

    async def quote(self, product: Product):
        if self.quote_started is not None:
            self.quote_started.set()
            assert self.quote_continue is not None
            await self.quote_continue.wait()
        if self.quote_error:
            raise TimeoutError
        if self.name == "tronow" and product.minutes != 60:
            return None
        return Offer(self.name, self.price, datetime.now(TIMEZONE) + timedelta(seconds=20))

    def body(self, product: Product, business_id: str):
        return json.dumps(
            {
                "key": business_id,
                "energy": product.energy,
                "minutes": product.minutes,
                "address": product.address,
            }
        )

    def result(self, attempt):
        return PurchaseResult(
            self.name + "-" + attempt.business_id,
            self.state,
            self.actual_cost or self.price,
            datetime.now(TIMEZONE) + timedelta(minutes=20) if self.name == "tronow" else None,
        )

    async def submit(self, attempt):
        self.posts.append((attempt.business_id, attempt.idempotency_key, attempt.request_body))
        self.charges[attempt.business_id] = attempt.request_body
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return self.result(attempt)

    async def recover(self, attempt):
        self.queries.append(attempt.upstream_order_id or attempt.business_id)
        if attempt.business_id not in self.charges:
            return await self.submit(attempt)
        return self.result(attempt)

    async def close(self):
        pass


async def funded(factory, amount="10", user=1):
    async with factory() as session, session.begin():
        await upsert_user(session, user_id=user, first_name="测试用户", language_code="zh")
        await wallet.credit(
            session, user_id=user, amount=Decimal(amount), reference=f"initial-{user}"
        )


async def reserve(factory, key="request", price="4", minutes=60, user=1):
    async with factory() as session, session.begin():
        order = await rental.reserve_order(
            session,
            user_id=user,
            request_key=key,
            recipient_address=ADDRESS,
            energy_amount=65000,
            duration_minutes=minutes,
            price=Decimal(price),
        )
        return order.id


async def due(factory, order_id, *, expire_lease=False):
    async with factory() as session, session.begin():
        order = await session.get(Order, order_id)
        assert order is not None
        order.next_run_at = datetime.now(TIMEZONE) - timedelta(seconds=1)
        if expire_lease:
            order.lease_until = order.next_run_at


def worker(factory, *providers, send=None, retries=1):
    return OrderWorker(
        factory,
        {p.name: p for p in providers},
        RentalSettings(enabled=True, batch_size=1, quote_retry_limit=retries),
        send,
    )


async def assert_money(factory, available, frozen, captures=0, releases=0):
    async with factory() as session:
        account = await session.get(Wallet, 1)
        assert account is not None
        assert account.available == Decimal(available)
        assert account.frozen == Decimal(frozen)
        deltas = (
            await session.execute(
                select(
                    func.sum(WalletEntry.available_delta), func.sum(WalletEntry.frozen_delta)
                ).where(WalletEntry.user_id == 1)
            )
        ).one()
        assert deltas == (account.available, account.frozen)
        for prefix, count in (("capture:", captures), ("release:", releases)):
            actual = await session.scalar(
                select(func.count())
                .select_from(WalletEntry)
                .where(WalletEntry.key.startswith(prefix))
            )
            assert actual == count


async def test_credit_idempotency_and_conflict(db_factory):
    await funded(db_factory)
    async with db_factory() as session, session.begin():
        await wallet.credit(session, user_id=1, amount=Decimal("10"), reference="initial-1")
        with pytest.raises(wallet.WalletError, match="凭证"):
            await wallet.credit(session, user_id=1, amount=Decimal("11"), reference="initial-1")
    await assert_money(db_factory, "10", "0")


async def test_credit_reference_cannot_pay_two_users(db_factory):
    await funded(db_factory)
    await funded(db_factory, user=2)
    async with db_factory() as session, session.begin():
        with pytest.raises(wallet.WalletError):
            await wallet.credit(session, user_id=2, amount=Decimal("10"), reference="initial-1")


async def test_concurrent_same_request_freezes_once(db_factory):
    await funded(db_factory)
    ids = await asyncio.gather(*(reserve(db_factory) for _ in range(4)))
    assert len(set(ids)) == 1
    await assert_money(db_factory, "6", "4")
    with pytest.raises(rental.RentalError, match="相同请求"):
        await reserve(db_factory, price="5")
    await assert_money(db_factory, "6", "4")


async def test_concurrent_orders_cannot_overspend(db_factory):
    await funded(db_factory)
    outcomes = await asyncio.gather(
        *(reserve(db_factory, key=f"r{i}", price="7") for i in range(2)), return_exceptions=True
    )
    assert sum(isinstance(r, int) for r in outcomes) == 1
    assert sum(isinstance(r, wallet.WalletError) for r in outcomes) == 1
    await assert_money(db_factory, "3", "7")
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1


async def test_cheapest_supplier_and_exactly_once_settlement(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    cheap, expensive = FakeProvider(price="2"), FakeProvider("tronbid", price="3")
    service = worker(db_factory, cheap, expensive)
    await asyncio.gather(service.tick(), worker(db_factory, cheap, expensive).tick())
    await assert_money(db_factory, "6", "0", captures=1)
    assert len(cheap.posts) == 1 and not expensive.posts
    async with db_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status is OrderStatus.ACTIVE
        assert order.purchase_cost == Decimal("2") and order.price == Decimal("4")
        assert await session.scalar(select(func.count()).select_from(PurchaseAttempt)) == 1


async def test_minutes_product_excludes_tronow(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory, minutes=15)
    short, hourly = FakeProvider("tronbid", price="3"), FakeProvider(price="1")
    service = worker(db_factory, hourly, short)
    await service.tick()
    assert not hourly.posts and len(short.posts) == 1
    await assert_money(db_factory, "6", "0", captures=1)
    async with db_factory() as session:
        order = await session.get(Order, order_id)
        assert order.duration_minutes == 15 and order.duration_hours is None
        assert order.expires_at is None
    short.state = "expired"
    await due(db_factory, order_id)
    await service.tick()
    async with db_factory() as session:
        assert (await session.get(Order, order_id)).status is OrderStatus.EXPIRED
    await assert_money(db_factory, "6", "0", captures=1)


async def test_no_affordable_quote_releases_frozen_funds(db_factory):
    await funded(db_factory)
    await reserve(db_factory)
    provider = FakeProvider(price="5")
    await worker(db_factory, provider, FakeProvider("tronbid", price="6")).tick()
    assert not provider.posts
    await assert_money(db_factory, "10", "0", releases=1)


async def test_quote_failure_retries_before_release(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.quote_error = True
    service = worker(db_factory, provider, FakeProvider("tronbid", price="6"), retries=2)
    await service.tick()
    await assert_money(db_factory, "6", "4")
    await due(db_factory, order_id)
    await service.tick()
    await assert_money(db_factory, "10", "0", releases=1)


async def test_timeout_recovers_original_without_supplier_switch(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    original, alternate = FakeProvider(), FakeProvider("tronbid", price="3")
    original.error = TimeoutError()
    await worker(db_factory, original, alternate).tick()
    await assert_money(db_factory, "6", "4")
    async with db_factory() as session, session.begin():
        order = await session.get(Order, order_id)
        with pytest.raises(rental.RentalError, match="尚未确认"):
            await rental.release_order(session, order, reason="UNSAFE")
    await due(db_factory, order_id)
    await worker(db_factory, original, alternate).tick()
    assert len(original.charges) == 1 and len(original.posts) == 1 and not alternate.posts
    await assert_money(db_factory, "6", "0", captures=1)


async def test_crash_after_remote_acceptance_recovers(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await worker(db_factory, provider).tick()
    await assert_money(db_factory, "6", "4")
    await due(db_factory, order_id, expire_lease=True)
    await worker(db_factory, provider).tick()
    assert len(provider.charges) == 1 and len(provider.posts) == 1
    await assert_money(db_factory, "6", "0", captures=1)


async def test_failed_provider_falls_back_only_after_confirmation(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    original, alternate = FakeProvider(state="pending"), FakeProvider("tronbid", price="3")
    service = worker(db_factory, original, alternate)
    await service.tick()
    assert not alternate.posts
    original.state = "failed"
    await due(db_factory, order_id)
    await service.tick()
    assert not alternate.posts  # 本轮只持久化明确失败结果
    await due(db_factory, order_id)
    await service.tick()
    assert len(original.posts) == len(alternate.posts) == 1
    await assert_money(db_factory, "6", "0", captures=1)
    async with db_factory() as session:
        attempts = list(
            (await session.scalars(select(PurchaseAttempt).order_by(PurchaseAttempt.id))).all()
        )
        assert [a.state for a in attempts] == ["failed", "succeeded"]
        assert attempts[0].business_id != attempts[1].business_id


async def test_all_confirmed_failures_release_once(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider(state="failed")
    service = worker(db_factory, provider)
    await service.tick()
    await due(db_factory, order_id)
    await service.tick()
    await service.tick()
    await assert_money(db_factory, "10", "0", releases=1)
    assert len(provider.posts) == 1


async def test_reviewing_keeps_funds_and_never_switches(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider, alternate = FakeProvider(state="reviewing"), FakeProvider("tronbid", price="3")
    service = worker(db_factory, provider, alternate)
    await service.tick()
    for _ in range(2):
        await due(db_factory, order_id)
        await service.tick()
    await assert_money(db_factory, "6", "4")
    assert len(provider.posts) == 1 and not alternate.posts


async def test_cancel_during_quote_prevents_post(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.quote_started, provider.quote_continue = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        worker(db_factory, provider, FakeProvider("tronbid", price="3")).tick()
    )
    await asyncio.wait_for(provider.quote_started.wait(), 5)
    async with db_factory() as session, session.begin():
        await rental.cancel_order(session, user_id=1, order_id=order_id)
    provider.quote_continue.set()
    await task
    assert not provider.posts
    await assert_money(db_factory, "10", "0", releases=1)


async def test_other_user_cannot_cancel(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    async with db_factory() as session, session.begin():
        with pytest.raises(rental.RentalError, match="不存在"):
            await rental.cancel_order(session, user_id=2, order_id=order_id)
    await assert_money(db_factory, "6", "4")


async def test_actual_cost_overrun_does_not_charge_user_more(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.actual_cost = Decimal("6")
    await worker(db_factory, provider).tick()
    await assert_money(db_factory, "6", "0", captures=1)
    async with db_factory() as session:
        order = await session.get(Order, order_id)
        assert order.purchase_cost == Decimal("6")
        assert order.last_error == "COST_OVERRUN"


async def test_notification_retry_does_not_repeat_settlement(db_factory):
    await funded(db_factory)
    await reserve(db_factory)
    send = AsyncMock(side_effect=[TimeoutError(), None])
    service = worker(db_factory, FakeProvider(), send=send)
    await service.tick()
    if send.await_count == 0:
        await service.deliver_notifications()  # 通知调度独立于采购,无需同一轮完成
    async with db_factory() as session, session.begin():
        event = await session.scalar(select(OrderNotification))
        assert event.sent_at is None
        event.next_run_at = datetime.now(TIMEZONE)
    await service.deliver_notifications()
    assert send.await_count == 2
    await assert_money(db_factory, "6", "0", captures=1)


async def test_webhook_before_post_response_can_wake_by_business_id(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await worker(db_factory, provider).tick()
    async with db_factory() as session, session.begin():
        attempt = await session.scalar(select(PurchaseAttempt))
        assert attempt.upstream_order_id is None
        assert await wake_tronow(session, "ord_early", attempt.business_id)
    await due(db_factory, order_id, expire_lease=True)
    await worker(db_factory, provider).tick()
    await assert_money(db_factory, "6", "0", captures=1)


async def test_unknown_response_cannot_be_refunded_as_rejection(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.error = TimeoutError()
    service = worker(db_factory, provider)
    await service.tick()
    provider.recover = AsyncMock(side_effect=TronowApiError("INVALID_CREDENTIALS", 401))
    await due(db_factory, order_id)
    await service.tick()
    await assert_money(db_factory, "6", "4")
    async with db_factory() as session:
        assert (await session.scalar(select(PurchaseAttempt))).state == "reviewing"


async def test_wallet_database_nonnegative_constraint(db_factory):
    await funded(db_factory)
    with pytest.raises(IntegrityError):
        async with db_factory() as session, session.begin():
            account = await session.get(Wallet, 1)
            account.available = Decimal("-1")
    await assert_money(db_factory, "10", "0")


@pytest.mark.parametrize("provider_name", ["tronow", "tronbid"])
@pytest.mark.parametrize("lost_response", [False, True])
async def test_real_adapter_http_and_database_lifecycle(db_factory, provider_name, lost_response):
    remote = {
        "bodies": [],
        "charges": {},
        "fulfilled": False,
        "lost": lost_response,
        "read_calls": 0,
    }

    def response(data, status=200):
        envelope = {"code": "OK", "data": data} if provider_name == "tronow" else data
        return web.json_response(envelope, status=status)

    def full_order(body):
        if provider_name == "tronow":
            return {
                "order_id": "ord_test",
                "client_order_id": body["client_order_id"],
                "status": "SUCCESS" if remote["fulfilled"] else "PROCESSING",
                "receiver_address": body["receiver_address"],
                "resource_amount": 65000,
                "duration": "1h",
                "amount_sun": "2000000",
                "currency": "TRX",
                "created_at": "2026-09-20T00:00:00+08:00",
                "txid": "synthetic-tx",
                "confirmed_at": datetime.now(TIMEZONE).isoformat(),
                "lease_expires_at": (datetime.now(TIMEZONE) + timedelta(minutes=40)).isoformat(),
            }
        return {
            "id": "synthetic-id",
            "status": "delegated" if remote["fulfilled"] else "paid",
            "payment_mode": "balance",
            "amount_trx": "2.000000",
            "energy_amount": 65000,
            "duration_minutes": body["duration_minutes"],
            "target_address": body["target_address"],
            "expires_at": "2000-01-01T00:00:00Z",
        }

    async def endpoint(request):
        path = request.path
        if path.endswith("balance"):
            remote["read_calls"] += 1
            return response(
                {
                    "currency": "TRX",
                    "available_balance_sun": "100000000",
                    "reserved_balance_sun": "0",
                    "total_balance_sun": "100000000",
                    "updated_at": "2026-09-20T00:00:00+08:00",
                    "balance_trx": "100",
                }
            )
        if path.endswith("quote"):
            remote["read_calls"] += 1
            return response(
                {
                    "resource_amount": 65000,
                    "duration": "1h",
                    "price_sun": "2000000",
                    "currency": "TRX",
                    "priced_at": datetime.now(TIMEZONE).isoformat(),
                    "price_trx": "2.000000",
                    "available": True,
                    "expires_in_sec": 30,
                }
            )
        if request.method == "POST":
            raw = await request.read()
            body = json.loads(raw)
            if provider_name == "tronbid":
                assert body["payment_mode"] == "balance"
                key = body["idempotency_key"]
            else:
                key = request.headers["Idempotency-Key"]
            remote["bodies"].append(raw)
            remote["charges"][key] = body
            if remote["lost"]:
                remote["lost"] = False
                return web.Response(status=502, body=b"gateway lost response")
            if provider_name == "tronow":
                return response(
                    {
                        "order_id": "ord_test",
                        "client_order_id": body["client_order_id"],
                        "status": "PROCESSING",
                        "reserved_amount_sun": "2000000",
                        "currency": "TRX",
                        "created_at": "2026-09-20T00:00:00+08:00",
                    },
                    201,
                )
            return response(full_order(body))
        body = next(iter(remote["charges"].values()))
        return response(full_order(body))

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", endpoint)
    await funded(db_factory)
    order_id = await reserve(db_factory)
    async with TestClient(TestServer(app)) as server:
        provider = (
            TronowProvider(
                TronowClient(TronowSettings(base_url=str(server.make_url("/openapi/v1"))))
            )
            if provider_name == "tronow"
            else TronbidProvider(
                TronbidClient(TronbidSettings(base_url=str(server.make_url("/api/v2/quick-rent"))))
            )
        )
        service = worker(db_factory, provider)
        try:
            await service.tick()
            await assert_money(db_factory, "6", "4")
            remote["fulfilled"] = True
            await due(db_factory, order_id)
            await worker(db_factory, provider).tick()
            await assert_money(db_factory, "6", "0", captures=1)
            assert remote["read_calls"] == 0
            assert len(remote["charges"]) == 1
            assert len(set(remote["bodies"])) == 1
            expected_posts = 2 if provider_name == "tronbid" and lost_response else 1
            assert len(remote["bodies"]) == expected_posts
            async with db_factory() as session:
                attempt = await session.scalar(select(PurchaseAttempt))
                assert attempt.request_body.encode() == remote["bodies"][0]
                assert attempt.quoted_cost is None
                order = await session.get(Order, order_id)
                assert order.status is OrderStatus.ACTIVE
                if provider_name == "tronbid":
                    assert order.expires_at is None  # 不把付款截止日期当租赁到期
        finally:
            await provider.close()


async def test_stale_worker_cannot_overwrite_new_lease(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    old, new = worker(db_factory, provider), worker(db_factory, provider)
    first = await old._claim()
    assert first is not None
    await due(db_factory, order_id, expire_lease=True)
    second = await new._claim()
    assert second is not None and second[1] != first[1]
    await old.process(*first)
    assert not provider.posts
    await new.process(*second)
    assert len(provider.posts) == 1
    await assert_money(db_factory, "6", "0", captures=1)


async def test_expired_without_delivery_history_is_not_failure_proof(db_factory):
    await funded(db_factory)
    await reserve(db_factory)
    provider = FakeProvider("tronbid", state="expired")
    alternate = FakeProvider(price="3")
    await worker(db_factory, provider, alternate).tick()
    await assert_money(db_factory, "6", "4")
    assert not alternate.posts
    async with db_factory() as session:
        assert (await session.scalar(select(PurchaseAttempt))).state == "reviewing"


async def test_concurrent_credit_reference_has_one_winner(db_factory):
    await funded(db_factory)
    await funded(db_factory, user=2)

    async def credit(user):
        async with db_factory() as session, session.begin():
            await wallet.credit(
                session, user_id=user, amount=Decimal("5"), reference="shared-proof"
            )
        return user

    results = await asyncio.gather(credit(1), credit(2), return_exceptions=True)
    assert sum(isinstance(r, int) for r in results) == 1
    async with db_factory() as session:
        assert await session.scalar(select(func.sum(Wallet.available))) == Decimal("25")
        assert await session.scalar(select(func.sum(WalletEntry.available_delta))) == Decimal("25")


async def test_quote_rate_limit_respects_retry_after(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    provider = FakeProvider()
    provider.quote = AsyncMock(side_effect=TronowApiError("RATE_LIMITED", 429, retry_after=120))
    before = datetime.now(TIMEZONE)
    await worker(db_factory, provider, FakeProvider("tronbid", price="6"), retries=3).tick()
    async with db_factory() as session:
        order = await session.get(Order, order_id)
        assert order.next_run_at >= before + timedelta(seconds=120)
    await assert_money(db_factory, "6", "4")
