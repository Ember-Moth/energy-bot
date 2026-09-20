"""TRONow webhook 验收:验签固定向量 + 端到端流转(需真实 PG 时走集成段)。"""

import hashlib
import hmac
import json
import os
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from energy_bot.config import TronowSettings
from energy_bot.db import create_engine_from_dsn, create_session_factory
from energy_bot.models import Base, Order, OrderStatus, UpstreamDelivery, User
from energy_bot.services import rental
from energy_bot.web.tronow import TronowWebhookView, verify_signature

WEBHOOK_SECRET = "whsec-test"
ADDRESS = "T" + "2" * 33


def _sign(secret: str, ts: str, delivery: str, event: str, body: bytes) -> str:
    canonical = f"{ts}.{delivery}.{event}".encode() + b"." + body
    return "v1=" + hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def _ts_now() -> str:
    return str(int(time.time()))


def _callback_body(
    *,
    order_id: str = "ord_x1",
    status: str = "SUCCESS",
    client_order_id: str = "eb-000001",
    txid: str | None = "tx-hash-1",
) -> bytes:
    payload: dict[str, Any] = {
        "event": "order.succeeded",
        "data": {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": status,
            "amount_sun": "3250000",
            "txid": txid,
        },
    }
    return json.dumps(payload).encode()


def _build_app(secret: str, session_factory: Any) -> web.Application:
    app = web.Application()
    TronowWebhookView(_tronow_settings(secret), session_factory).register(
        app, "/upstream/tronow/webhook"
    )
    return app


def _tronow_settings(secret: str) -> TronowSettings:
    return TronowSettings(api_key="k", api_secret="s", webhook_secret=secret, timeout_seconds=5)


# --- 验签固定向量 ---


def test_verify_signature_accepts_valid() -> None:
    body = b'{"a":1}'
    sig = _sign(WEBHOOK_SECRET, "1700000000", "d1", "order.succeeded", body)
    assert verify_signature(
        WEBHOOK_SECRET,
        timestamp="1700000000",
        delivery_id="d1",
        event="order.succeeded",
        signature=sig,
        body=body,
    )


def test_verify_signature_rejects_tampering() -> None:
    body = b'{"a":1}'
    sig = _sign(WEBHOOK_SECRET, "1700000000", "d1", "order.succeeded", body)
    # 改动任一要素都必须失败:body / event / delivery / timestamp / secret
    tampered: list[dict[str, Any]] = [
        {"body": b'{"a":2}'},
        {"event": "order.failed"},
        {"delivery_id": "d2"},
        {"timestamp": "1700000001"},
    ]
    for overrides in tampered:
        kwargs: dict[str, Any] = {
            "timestamp": "1700000000",
            "delivery_id": "d1",
            "event": "order.succeeded",
            "signature": sig,
            "body": body,
        }
        kwargs.update(overrides)
        assert not verify_signature(WEBHOOK_SECRET, **kwargs), f"篡改 {overrides} 未被拒绝"
    assert not verify_signature(
        "other-secret",
        timestamp="1700000000",
        delivery_id="d1",
        event="order.succeeded",
        signature=sig,
        body=body,
    )
    assert not verify_signature(
        WEBHOOK_SECRET,
        timestamp="1700000000",
        delivery_id="d1",
        event="order.succeeded",
        signature=sig.replace("v1=", "v2="),
        body=body,
    )
    assert not verify_signature(
        WEBHOOK_SECRET,
        timestamp="1700000000",
        delivery_id="d1",
        event="order.succeeded",
        signature="not-a-signature",
        body=body,
    )
    # 非常数时间库的拼接攻击不适用(compare_digest 全串比较)
    assert not verify_signature(
        WEBHOOK_SECRET,
        timestamp="1700000000",
        delivery_id="d1",
        event="order.succeeded",
        signature="",
        body=body,
    )


def test_verify_signature_rejects_missing_parts() -> None:
    body = b"{}"
    for kwargs in (
        {"timestamp": ""},
        {"delivery_id": ""},
        {"event": ""},
        {"signature": "v1=" + "0" * 64},
    ):
        base = {"timestamp": "1700000000", "delivery_id": "d1", "event": "e", "signature": "v1=x"}
        base.update(kwargs)
        assert not verify_signature(WEBHOOK_SECRET, body=body, **base), f"缺字段 {kwargs} 未被拒绝"


# --- 端到端(真实 PG:去重 + 状态流转;无 DSN 时跳过) ---


@pytest.fixture
async def webhook_client():
    if not os.environ.get("ENERGY_BOT_TEST_DSN"):
        pytest.skip("需要 ENERGY_BOT_TEST_DSN 指向可用的 PostgreSQL")
    engine = create_engine_from_dsn(os.environ["ENERGY_BOT_TEST_DSN"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    # 预置用户 + 一条 delegating 订单
    async with factory() as session:
        session.add(User(id=1, first_name="回调测试用户"))
        session.add(
            Order(
                user_id=1,
                recipient_address=ADDRESS,
                energy_amount=65000,
                duration_hours=1,
                price=1,
                status=OrderStatus.DELEGATING,
                provider="tronow",
                upstream_order_id="ord_x1",
            )
        )
        await session.commit()

    app = _build_app(WEBHOOK_SECRET, factory)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, factory
    await client.close()
    await engine.dispose()


def _post(
    client: TestClient, body: bytes, delivery: str, event: str = "order.succeeded", **headers
):
    ts = _ts_now()
    sig = _sign(WEBHOOK_SECRET, ts, delivery, event, body)
    return client.post(
        "/upstream/tronow/webhook",
        data=body,
        headers={
            "X-Lease-Timestamp": ts,
            "X-Lease-Delivery": delivery,
            "X-Lease-Event": event,
            "X-Lease-Signature": sig,
            **headers,
        },
    )


async def test_webhook_activates_order_and_dedups(webhook_client) -> None:
    http, factory = webhook_client
    body = _callback_body()

    resp = await _post(http, body, delivery="dlv-1")
    assert resp.status == 200
    assert (await resp.json())["status"] == "applied"

    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.ACTIVE
        assert order.upstream_txid == "tx-hash-1"
        assert order.expires_at is not None
        deliveries = len(
            (await session.execute(__import__("sqlalchemy").select(UpstreamDelivery)))
            .scalars()
            .all()
        )
    assert deliveries == 1

    # 相同 delivery 重投:幂等应答,不产生第二条去重记录
    resp = await _post(http, body, delivery="dlv-1")
    assert resp.status == 200
    assert (await resp.json())["status"] == "duplicate"


async def test_webhook_duplicate_event_is_idempotent(webhook_client) -> None:
    http, factory = webhook_client
    await _post(http, _callback_body(), delivery="dlv-1")
    # 新 delivery、相同业务事件:不应报错,状态保持 active
    resp = await _post(http, _callback_body(), delivery="dlv-2")
    assert resp.status == 200
    assert (await resp.json())["status"] == "applied"
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.ACTIVE


async def test_webhook_failed_event_transitions(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(
        http,
        _callback_body(status="FAILED", txid=None),
        delivery="dlv-f1",
        event="order.failed",
    )
    assert resp.status == 200
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.FAILED


async def test_webhook_rejects_bad_signature(webhook_client) -> None:
    http, factory = webhook_client
    body = _callback_body()
    ts = _ts_now()
    resp = await http.post(
        "/upstream/tronow/webhook",
        data=body,
        headers={
            "X-Lease-Timestamp": ts,
            "X-Lease-Delivery": "dlv-x",
            "X-Lease-Event": "order.succeeded",
            "X-Lease-Signature": "v1=" + "0" * 64,
        },
    )
    assert resp.status == 401
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.DELEGATING  # 未被篡改请求改动


async def test_webhook_rejects_stale_timestamp(webhook_client) -> None:
    http, _ = webhook_client
    body = _callback_body()
    old_ts = str(int(time.time()) - 3600)  # 1 小时前
    delivery, event = "dlv-old", "order.succeeded"
    sig = _sign(WEBHOOK_SECRET, old_ts, delivery, event, body)
    resp = await http.post(
        "/upstream/tronow/webhook",
        data=body,
        headers={
            "X-Lease-Timestamp": old_ts,
            "X-Lease-Delivery": delivery,
            "X-Lease-Event": event,
            "X-Lease-Signature": sig,
        },
    )
    assert resp.status == 401


async def test_webhook_ignores_nonterminal_status(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _callback_body(status="CONFIRMING"), delivery="dlv-c1")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ignored"
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.DELEGATING


async def test_webhook_unmatched_order_still_200(webhook_client) -> None:
    http, _ = webhook_client
    resp = await _post(http, _callback_body(order_id="ord_unknown"), delivery="dlv-u1")
    assert resp.status == 200
    assert (await resp.json())["status"] == "unmatched"


async def test_webhook_503_when_secret_missing() -> None:
    app = _build_app("", session_factory=None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/upstream/tronow/webhook", data=b"{}")
        assert resp.status == 503
    finally:
        await client.close()
