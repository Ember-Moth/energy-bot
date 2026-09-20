"""GMPay 回调端到端(真实 PG):入账、幂等、金额不符、unmatched 重投。"""

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import GmpaySettings
from energy_bot.models import (
    DepositOrder,
    DepositStatus,
    UpstreamDelivery,
    User,
    Wallet,
    WalletEntry,
)
from energy_bot.services.payment.gmpay import sign_params
from energy_bot.web.gmpay import GmpayWebhookView

SECRET = "gmpay-test-secret"
ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pid": "1000",
        "trade_id": "tid-1",
        "order_id": "dep-1-abcdef0123",
        "amount": 50.0,
        "actual_amount": 50.0,
        "receive_address": ADDRESS,
        "token": "trx",
        "block_transaction_id": "tx-hash-1",
        "status": 2,
    }
    payload.update(overrides)
    payload["signature"] = sign_params(payload, SECRET)
    return payload


def _build_app(secret: str, session_factory: Any) -> web.Application:
    app = web.Application()
    GmpayWebhookView(GmpaySettings(pid="1000", secret_key=secret), session_factory).register(
        app, "/payment/gmpay/notify"
    )
    return app


@pytest.fixture
async def webhook_client(db_factory):
    """预置用户 + 一条 created 充值单(库由 db_factory 经 Alembic 升级并逐例清空)。"""
    async with db_factory() as session:
        session.add(User(id=1, first_name="充值用户"))
        session.add(
            DepositOrder(
                user_id=1,
                order_id="dep-1-abcdef0123",
                trade_id="tid-1",
                fiat_amount=Decimal("50"),
                expected_amount=Decimal("50"),
                receive_address=ADDRESS,
                status=DepositStatus.CREATED,
            )
        )
        await session.commit()
    client = TestClient(TestServer(_build_app(SECRET, db_factory)), timeout=ClientTimeout(total=5))
    await client.start_server()
    try:
        yield client, db_factory
    finally:
        await client.close()


def _post(client: TestClient, payload: dict[str, Any]):
    body = json.dumps(payload).encode()
    return client.post(
        "/payment/gmpay/notify", data=body, headers={"Content-Type": "application/json"}
    )


async def _wallet(factory: async_sessionmaker[AsyncSession]) -> Wallet:
    async with factory() as session:
        account = await session.get(Wallet, 1)
        assert account is not None
        return account


async def test_callback_credits_wallet_and_dedups(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _payload())
    assert resp.status == 200
    assert await resp.text() == "ok"
    account = await _wallet(factory)
    assert account.available == Decimal("50")  # 足额入账
    async with factory() as session:
        order = await session.scalar(select(DepositOrder).where(DepositOrder.trade_id == "tid-1"))
        assert order is not None and order.status is DepositStatus.PAID
        assert order.block_transaction_id == "tx-hash-1"
        assert order.paid_at is not None
        deliveries = (
            await session.execute(select(func.count()).select_from(UpstreamDelivery))
        ).scalar_one()
    assert deliveries == 1

    # 重复投递:幂等应答,不重复入账、不新增去重记录
    resp = await _post(http, _payload())
    assert resp.status == 200
    account = await _wallet(factory)
    assert account.available == Decimal("50")
    async with factory() as session:
        deliveries = (
            await session.execute(select(func.count()).select_from(UpstreamDelivery))
        ).scalar_one()
    assert deliveries == 1


async def test_callback_rejects_bad_signature(webhook_client) -> None:
    http, factory = webhook_client
    payload = {**_payload(), "signature": "0" * 64}
    resp = await _post(http, payload)
    assert resp.status == 401
    async with factory() as session:
        assert await session.get(Wallet, 1) is None  # 未入账


async def test_callback_amount_mismatch_goes_manual(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _payload(actual_amount=49.5))
    assert resp.status == 200  # 已落库转人工,正常应答不再重投
    async with factory() as session:
        order = await session.scalar(select(DepositOrder).where(DepositOrder.trade_id == "tid-1"))
        assert order is not None and order.status is DepositStatus.FAILED
        assert await session.get(Wallet, 1) is None  # 金额不符:不入账
        seen = await session.scalar(
            select(UpstreamDelivery.delivery_id).where(
                UpstreamDelivery.delivery_id == "gmpay:tid-1"
            )
        )
    assert seen is not None  # 已去重,重投递直接 ok


async def test_callback_unmatched_gets_503_for_redelivery(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _payload(trade_id="tid-unknown", order_id="dep-x"))
    assert resp.status == 503  # 非 2xx 网关才会重投
    async with factory() as session:
        seen = await session.scalar(
            select(UpstreamDelivery.delivery_id).where(
                UpstreamDelivery.delivery_id == "gmpay:tid-unknown"
            )
        )
    assert seen is None  # 不落去重,重投时本地落库后即可匹配


async def test_callback_503_when_secret_missing() -> None:
    app = _build_app("", session_factory=None)
    client = TestClient(TestServer(app), timeout=ClientTimeout(total=5))
    await client.start_server()
    try:
        resp = await client.post("/payment/gmpay/notify", data=b"{}")
        assert resp.status == 503
    finally:
        await client.close()


async def test_callback_rejects_oversized_body() -> None:
    app = _build_app(SECRET, session_factory=None)
    client = TestClient(TestServer(app), timeout=ClientTimeout(total=5))
    await client.start_server()
    try:
        resp = await client.post("/payment/gmpay/notify", data=b" " * (70 * 1024))
        assert resp.status == 413
    finally:
        await client.close()


@pytest.mark.parametrize("instance", range(3))
async def test_repeated_ack_without_database(instance: int) -> None:
    """不用 PG 也能复现旧 bug;跨请求和跨应用实例都必须发送完整的 ok。"""
    async with TestClient(
        TestServer(_build_app(SECRET, None)), timeout=ClientTimeout(total=5)
    ) as http:
        for _ in range(3):
            response = await _post(http, _payload(status=1, trade_id=f"ignored-{instance}"))
            assert response.status == 200
            assert await response.text() == "ok"


async def test_concurrent_duplicate_callbacks_all_ack_once_credit(webhook_client) -> None:
    http, factory = webhook_client
    responses = await asyncio.gather(*(_post(http, _payload()) for _ in range(4)))
    assert all(response.status == 200 for response in responses)
    assert await asyncio.gather(*(response.text() for response in responses)) == ["ok"] * 4
    assert (await _wallet(factory)).available == Decimal("50")
    async with factory() as session:
        entries = await session.scalar(
            select(func.count())
            .select_from(WalletEntry)
            .where(
                WalletEntry.key == "credit:gmpay:tid-1",
            )
        )
        assert entries == 1
        assert await session.scalar(select(func.count()).select_from(UpstreamDelivery)) == 1


async def test_distinct_paid_callbacks_each_get_response(webhook_client) -> None:
    http, factory = webhook_client
    async with factory() as session, session.begin():
        session.add(
            DepositOrder(
                user_id=1,
                order_id="dep-1-second",
                trade_id="tid-2",
                fiat_amount=Decimal("50"),
                expected_amount=Decimal("50"),
                receive_address=ADDRESS,
                status=DepositStatus.CREATED,
            )
        )
    for trade_id, order_id in (("tid-1", "dep-1-abcdef0123"), ("tid-2", "dep-1-second")):
        response = await _post(http, _payload(trade_id=trade_id, order_id=order_id))
        assert response.status == 200 and await response.text() == "ok"
    assert (await _wallet(factory)).available == Decimal("100")
