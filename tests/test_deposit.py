"""充值下单、原单恢复和查询/回调竞态:使用真实 PostgreSQL。"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from aiohttp import ClientConnectionError, ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from energy_bot.config import GmpaySettings
from energy_bot.handlers.deposit import deposit, deposit_status
from energy_bot.middlewares.db import DbSessionMiddleware
from energy_bot.models import (
    DepositOrder,
    DepositStatus,
    UpstreamDelivery,
    User,
    Wallet,
    WalletEntry,
)
from energy_bot.services.payment.gmpay import (
    GmpayApiError,
    GmpayClient,
    GmpayTransaction,
    sign_params,
)
from energy_bot.web.gmpay import GmpayWebhookView

ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
SECRET = "deposit-test-secret"
NOTIFY_URL = "https://bot.example.com/payment/gmpay/notify"


def _message(user_id: int = 1, message_id: int = 1) -> Message:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=user_id, full_name="充值测试", language_code="zh")
    message.chat = SimpleNamespace(id=user_id, type="private")
    message.message_id = message_id
    return cast(Message, message)


def _transaction(order_id: str, **overrides: Any) -> GmpayTransaction:
    return GmpayTransaction(
        **{
            "order_id": order_id,
            "trade_id": "tid-review",
            "amount": Decimal("50"),
            "actual_amount": Decimal("50"),
            "receive_address": ADDRESS,
            "status": 1,
            "expiration_time": 2100000000,
            **overrides,
        }
    )


def _client() -> AsyncMock:
    client = AsyncMock(spec=GmpayClient)
    client.create_transaction.side_effect = lambda **values: _transaction(values["order_id"])
    return client


async def _submit(
    factory, client: GmpayClient | AsyncMock, message: Message, amount: str = "50"
) -> None:
    async def handle(event, data):
        await deposit(
            event,
            CommandObject(command="deposit", args=amount),
            data["session"],
            cast(GmpayClient, client),
            NOTIFY_URL,
        )

    await DbSessionMiddleware(factory)(handle, message, {})


async def _callback(factory, order_id: str, **overrides: Any) -> int:
    payload = {
        "pid": "1000",
        "trade_id": "tid-review",
        "order_id": order_id,
        "amount": "50",
        "actual_amount": "50",
        "receive_address": ADDRESS,
        "token": "trx",
        "block_transaction_id": "confirmed-chain-tx",
        "status": 2,
        **overrides,
    }
    payload["signature"] = sign_params(payload, SECRET)
    app = web.Application()
    GmpayWebhookView(GmpaySettings(pid="1000", secret_key=SECRET), factory).register(app, "/notify")
    async with TestClient(TestServer(app), timeout=ClientTimeout(total=5)) as http:
        response = await http.post("/notify", json=payload)
        if response.status == 200:
            assert await response.text() == "ok"
        return response.status


async def _order(factory) -> DepositOrder:
    async with factory() as session:
        order = await session.scalar(select(DepositOrder))
        assert order is not None
        return order


async def _assert_credited_once(factory, amount: Decimal) -> None:
    async with factory() as session:
        account = await session.get(Wallet, 1)
        assert account is not None and account.available == amount
        assert await session.scalar(select(func.count()).select_from(WalletEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(UpstreamDelivery)) == 1


async def test_intent_committed_before_gateway_and_message_replay_reuses_order(db_factory):
    client = _client()

    async def create(**values):
        visible = await _order(db_factory)
        assert visible.order_id == values["order_id"] and visible.trade_id is None
        return _transaction(values["order_id"])

    client.create_transaction.side_effect = create
    for _ in range(2):
        message = _message()
        await _submit(db_factory, client, message)
        assert ADDRESS in cast(AsyncMock, message.answer).call_args.args[0]
    client.create_transaction.assert_awaited_once()
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DepositOrder)) == 1


async def test_concurrent_first_user_and_message_replay_never_posts_twice(db_factory):
    client = _client()
    started, release = asyncio.Event(), asyncio.Event()

    async def create(**values):
        started.set()
        await release.wait()
        return _transaction(values["order_id"])

    client.create_transaction.side_effect = create
    async with asyncio.timeout(5):
        first = asyncio.create_task(_submit(db_factory, client, _message()))
        try:
            await started.wait()
            duplicate = _message()
            await _submit(db_factory, client, duplicate)
            assert "待核对" in cast(AsyncMock, duplicate.answer).call_args.args[0]
        finally:
            release.set()
            await first
    client.create_transaction.assert_awaited_once()
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DepositOrder)) == 1
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_simultaneous_new_user_updates_share_one_order(db_factory):
    client = _client()
    async with asyncio.timeout(5):
        await asyncio.gather(*(_submit(db_factory, client, _message()) for _ in range(4)))
    client.create_transaction.assert_awaited_once()
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DepositOrder)) == 1
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_same_message_cannot_change_amount(db_factory):
    client = _client()
    await _submit(db_factory, client, _message())
    changed = _message()
    await _submit(db_factory, client, changed, "51")
    client.create_transaction.assert_awaited_once()
    assert "不同充值金额" in cast(AsyncMock, changed.answer).call_args.args[0]
    assert (await _order(db_factory)).fiat_amount == Decimal("50")


async def test_reply_failure_preserves_receipt_for_telegram_redelivery(db_factory):
    client = _client()
    message = _message()
    cast(AsyncMock, message.answer).side_effect = RuntimeError("synthetic reply failure")
    with pytest.raises(RuntimeError, match="reply failure"):
        await _submit(db_factory, client, message)
    assert (await _order(db_factory)).trade_id == "tid-review"
    await _submit(db_factory, client, _message())
    client.create_transaction.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), ClientConnectionError(), GmpayApiError("HTTP_ERROR", status=502)],
)
async def test_unknown_submission_preserved_and_recovered_by_signed_callback(db_factory, error):
    client = _client()
    client.create_transaction.side_effect = error
    message = _message()
    await _submit(db_factory, client, message)
    order = await _order(db_factory)
    assert order.status is DepositStatus.CREATED and order.trade_id is None
    assert "待核对" in cast(AsyncMock, message.answer).call_args.args[0]
    await _submit(db_factory, client, _message())
    client.create_transaction.assert_awaited_once()
    # 网关交易锁可微增应付金额;验签后的原单回调恢复真实应付金额。
    responses = await asyncio.gather(
        *(_callback(db_factory, order.order_id, actual_amount="50.01") for _ in range(3))
    )
    assert responses == [200, 200, 200]
    paid = await _order(db_factory)
    assert paid.status is DepositStatus.PAID and paid.expected_amount == Decimal("50.01")
    assert paid.block_transaction_id == "confirmed-chain-tx"
    await _assert_credited_once(db_factory, Decimal("50.01"))
    await _submit(db_factory, client, _message())
    client.create_transaction.assert_awaited_once()


async def test_http_timeout_after_gateway_acceptance_can_recover_original_order(db_factory):
    received: dict[str, Any] = {}
    release = asyncio.Event()

    async def create(request: web.Request) -> web.Response:
        received.update(await request.json())
        assert received["amount"] == 50
        assert received["currency"] == "trx"
        assert received["notify_url"] == NOTIFY_URL
        assert sign_params(received, SECRET) == received["signature"]
        visible = await _order(db_factory)
        assert visible.order_id == received["order_id"] and visible.trade_id is None
        await release.wait()  # 已受理但响应丢失,客户端先超时
        return web.json_response({"code": 200, "data": {}})

    app = web.Application()
    app.router.add_post("/payments/gmpay/v1/order/create-transaction", create)
    async with TestClient(TestServer(app)) as http:
        async with GmpayClient(
            GmpaySettings(base_url=str(http.make_url("")), secret_key=SECRET, timeout_seconds=0.2)
        ) as client:
            try:
                message = _message()
                await _submit(db_factory, client, message)
                assert "待核对" in cast(AsyncMock, message.answer).call_args.args[0]
                assert (await _order(db_factory)).order_id == received["order_id"]
                assert await _callback(db_factory, received["order_id"]) == 200
                await _assert_credited_once(db_factory, Decimal("50"))
            finally:
                release.set()


async def test_cancelled_submission_keeps_committed_intent(db_factory):
    client = _client()
    client.create_transaction.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    assert order.trade_id is None and order.status is DepositStatus.CREATED
    await _submit(db_factory, client, _message())
    client.create_transaction.assert_awaited_once()
    assert await _callback(db_factory, order.order_id) == 200
    await _assert_credited_once(db_factory, Decimal("50"))


@pytest.mark.parametrize("result", ["response", "timeout"])
async def test_callback_before_create_response_preserves_paid_state(db_factory, result):
    client = _client()

    async def create(**values):
        assert await _callback(db_factory, values["order_id"], actual_amount="50.01") == 200
        if result == "timeout":
            raise TimeoutError
        return _transaction(values["order_id"], actual_amount=Decimal("50.01"))

    client.create_transaction.side_effect = create
    message = _message()
    await _submit(db_factory, client, message)
    paid = await _order(db_factory)
    assert paid.status is DepositStatus.PAID
    assert paid.block_transaction_id == "confirmed-chain-tx"
    assert "已入账" in cast(AsyncMock, message.answer).call_args.args[0]
    await _assert_credited_once(db_factory, Decimal("50.01"))


@pytest.mark.parametrize("status", [1, 2, 3, None])
async def test_callback_during_status_query_cannot_be_overwritten(db_factory, status):
    client = _client()
    await _submit(db_factory, client, _message())
    original = await _order(db_factory)
    paid_at = None

    async def check(trade_id):
        nonlocal paid_at
        assert trade_id == "tid-review"
        assert await _callback(db_factory, original.order_id) == 200
        paid_at = (await _order(db_factory)).paid_at
        if status is None:
            raise TimeoutError
        return status

    client.check_status.side_effect = check
    message = _message()
    async with db_factory() as session:
        # 强引用保持旧 ORM 对象,确保行锁查询确实刷新 identity map。
        stale = await session.get(DepositOrder, original.id)
        await deposit_status(
            message,
            CommandObject(command="deposit_status", args=str(original.id)),
            session,
            cast(GmpayClient, client),
        )
        assert stale is not None and stale.status is DepositStatus.PAID
    paid = await _order(db_factory)
    assert paid.status is DepositStatus.PAID and paid.paid_at == paid_at
    assert paid.block_transaction_id == "confirmed-chain-tx"
    assert "已入账" in cast(AsyncMock, message.answer).call_args.args[0]
    await _assert_credited_once(db_factory, Decimal("50"))


@pytest.mark.parametrize("status, expected", [(2, DepositStatus.PAID), (3, DepositStatus.EXPIRED)])
async def test_status_query_still_applies_to_created_order(db_factory, status, expected):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    client.check_status.return_value = status
    async with db_factory() as session:
        await deposit_status(
            _message(),
            CommandObject(command="deposit_status", args=str(order.id)),
            session,
            cast(GmpayClient, client),
        )
    assert (await _order(db_factory)).status is expected


@pytest.mark.parametrize(
    "overrides, response_status",
    [
        ({"pid": "other-merchant"}, 401),
        ({"amount": "51"}, 400),
        ({"token": "usdt"}, 400),
        ({"receive_address": "invalid"}, 400),
        ({"order_id": "missing-order"}, 503),
    ],
)
async def test_unknown_submission_recovery_rejects_wrong_identity(
    db_factory, overrides, response_status
):
    client = _client()
    client.create_transaction.side_effect = TimeoutError
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    payload = {"order_id": order.order_id, **overrides}
    assert await _callback(db_factory, **payload) == response_status
    assert (await _order(db_factory)).trade_id is None
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WalletEntry)) == 0
        assert await session.scalar(select(func.count()).select_from(UpstreamDelivery)) == 0


async def test_wrong_response_identity_is_not_bound_and_never_reposted(db_factory):
    client = _client()
    client.create_transaction.return_value = _transaction("other-order")
    client.create_transaction.side_effect = None
    await _submit(db_factory, client, _message())
    await _submit(db_factory, client, _message())
    assert (await _order(db_factory)).trade_id is None
    client.create_transaction.assert_awaited_once()


async def test_deposit_precision_rejected_before_gateway(db_factory):
    client = _client()
    message = _message()
    await _submit(db_factory, client, message, "50.0000001")
    client.create_transaction.assert_not_awaited()
    assert "6 位小数" in cast(AsyncMock, message.answer).call_args.args[0]
    async with db_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DepositOrder)) == 0


async def _query(factory, client: AsyncMock, order: DepositOrder) -> None:
    async with factory() as session:
        await deposit_status(
            _message(),
            CommandObject(command="deposit_status", args=str(order.id)),
            session,
            cast(GmpayClient, client),
        )


async def _record_old_delivery(factory) -> None:
    """模拟旧代码已确认、但未完成入账或交易号回填的回调。"""
    async with factory() as session, session.begin():
        session.add(
            UpstreamDelivery(delivery_id="gmpay:tid-review", provider="gmpay", event="order.paid")
        )


@pytest.mark.parametrize("delivery_seen", [False, True])
async def test_paid_callback_recovers_expired_order_even_if_old_delivery_seen(
    db_factory, delivery_seen
):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    client.check_status.return_value = 3
    await _query(db_factory, client, order)
    assert (await _order(db_factory)).status is DepositStatus.EXPIRED
    if delivery_seen:
        await _record_old_delivery(db_factory)

    replies = await asyncio.gather(*(_callback(db_factory, order.order_id) for _ in range(4)))
    assert replies == [200] * 4
    paid = await _order(db_factory)
    assert paid.status is DepositStatus.PAID and paid.paid_at is not None
    assert paid.block_transaction_id == "confirmed-chain-tx"
    # 同一付款重投不能重复加款或刷新入账时间。
    assert await _callback(db_factory, order.order_id) == 200
    assert (await _order(db_factory)).paid_at == paid.paid_at
    await _assert_credited_once(db_factory, Decimal("50"))


@pytest.mark.parametrize("delivery_seen", [False, True])
async def test_callback_backfills_transaction_after_status_query_without_recredit(
    db_factory, delivery_seen
):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    client.check_status.return_value = 2
    await _query(db_factory, client, order)
    queried = await _order(db_factory)
    assert queried.status is DepositStatus.PAID and queried.block_transaction_id is None
    assert queried.paid_at is not None
    if delivery_seen:
        await _record_old_delivery(db_factory)

    replies = await asyncio.gather(*(_callback(db_factory, order.order_id) for _ in range(4)))
    assert replies == [200] * 4
    paid = await _order(db_factory)
    assert paid.block_transaction_id == "confirmed-chain-tx"
    assert paid.paid_at == queried.paid_at
    await _assert_credited_once(db_factory, Decimal("50"))


async def test_expired_order_can_reconcile_late_payment_and_then_backfill_transaction(db_factory):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    client.check_status.return_value = 3
    await _query(db_factory, client, order)
    assert (await _order(db_factory)).status is DepositStatus.EXPIRED
    client.check_status.return_value = 2
    await _query(db_factory, client, await _order(db_factory))
    queried = await _order(db_factory)
    assert queried.status is DepositStatus.PAID
    assert await _callback(db_factory, order.order_id) == 200
    paid = await _order(db_factory)
    assert paid.block_transaction_id == "confirmed-chain-tx"
    assert paid.paid_at == queried.paid_at
    await _assert_credited_once(db_factory, Decimal("50"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"token": "usdt"},
        {"actual_amount": "49.5"},
        {"block_transaction_id": "other-chain-tx"},
        {"block_transaction_id": "x" * 129},
    ],
)
async def test_paid_callback_with_conflicting_evidence_preserves_existing_credit(
    db_factory, overrides
):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    assert await _callback(db_factory, order.order_id) == 200
    original = await _order(db_factory)
    assert await _callback(db_factory, order.order_id, **overrides) == 400
    paid = await _order(db_factory)
    assert paid.status is DepositStatus.PAID
    assert paid.block_transaction_id == original.block_transaction_id
    assert paid.paid_at == original.paid_at
    await _assert_credited_once(db_factory, Decimal("50"))


async def test_callback_without_transaction_can_later_fill_it_after_delivery_seen(db_factory):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    assert await _callback(db_factory, order.order_id, block_transaction_id="") == 200
    original = await _order(db_factory)
    assert original.block_transaction_id is None
    assert await _callback(db_factory, order.order_id) == 200
    paid = await _order(db_factory)
    assert paid.block_transaction_id == "confirmed-chain-tx"
    assert paid.paid_at == original.paid_at
    # 空交易号的重投也不能擦掉已经补齐的凭证。
    assert await _callback(db_factory, order.order_id, block_transaction_id="") == 200
    assert (await _order(db_factory)).block_transaction_id == "confirmed-chain-tx"
    await _assert_credited_once(db_factory, Decimal("50"))


async def test_failed_deposit_still_requires_manual_review(db_factory):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    assert await _callback(db_factory, order.order_id, actual_amount="49.5") == 200
    assert (await _order(db_factory)).status is DepositStatus.FAILED
    assert await _callback(db_factory, order.order_id) == 200
    assert (await _order(db_factory)).status is DepositStatus.FAILED
    async with db_factory() as session:
        assert await session.get(Wallet, 1) is None
        assert await session.scalar(select(func.count()).select_from(WalletEntry)) == 0
        assert await session.scalar(select(func.count()).select_from(UpstreamDelivery)) == 1


async def test_historical_expired_status_with_existing_credit_preserves_payment_evidence(
    db_factory,
):
    client = _client()
    await _submit(db_factory, client, _message())
    order = await _order(db_factory)
    assert await _callback(db_factory, order.order_id) == 200
    original = await _order(db_factory)
    async with db_factory() as session, session.begin():
        historical = await session.get(DepositOrder, original.id)
        assert historical is not None
        # 模拟旧查询逻辑把 paid 覆盖为 expired,资金和支付凭证其实已落库。
        historical.status = DepositStatus.EXPIRED
    assert await _callback(db_factory, order.order_id, block_transaction_id="") == 200
    restored = await _order(db_factory)
    assert restored.status is DepositStatus.PAID
    assert restored.block_transaction_id == original.block_transaction_id
    assert restored.paid_at == original.paid_at
    await _assert_credited_once(db_factory, Decimal("50"))
