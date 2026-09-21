"""TRONow webhook 验收:验签固定向量 + 端到端流转(需真实 PG 时走集成段)。"""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from energy_bot.config import TIMEZONE, TronowSettings
from energy_bot.models import Order, OrderStatus, UpstreamDelivery, User
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
    txid: Any = "tx-hash-1",
) -> bytes:
    payload: dict[str, Any] = {
        "event": "order.failed" if status == "FAILED" else "order.succeeded",
        "data": {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": status,
            "amount_sun": "3250000",
            "txid": txid,
            "lease_expires_at": (datetime.now(TIMEZONE) + timedelta(hours=1)).isoformat(),
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
async def webhook_client(db_factory):
    factory = db_factory
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
        deliveries = len((await session.execute(select(UpstreamDelivery))).scalars().all())
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


async def test_webhook_rejects_nonterminal_status(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _callback_body(status="CONFIRMING"), delivery="dlv-c1")
    assert resp.status == 422
    assert (await resp.json())["status"] == "invalid"
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.DELEGATING


async def test_webhook_unmatched_order_gets_503_for_redelivery(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _callback_body(order_id="ord_unknown"), delivery="dlv-u1")
    # 非 2xx 上游才会重投;且不落去重记录,重投时本地落库后即可匹配
    assert resp.status == 503
    assert (await resp.json())["status"] == "unmatched"
    async with factory() as session:
        seen = await session.scalar(
            select(UpstreamDelivery.delivery_id).where(UpstreamDelivery.delivery_id == "dlv-u1")
        )
    assert seen is None


async def test_webhook_concurrent_duplicate_answers_duplicate(
    webhook_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    http, factory = webhook_client
    # 模拟并发:同一 delivery 已被另一请求落库,但本请求的查重没读到(主键冲突兜底)
    async with factory() as session:
        session.add(
            UpstreamDelivery(delivery_id="dlv-race", provider="tronow", event="order.succeeded")
        )
        await session.commit()
    monkeypatch.setattr(TronowWebhookView, "_already_seen", AsyncMock(side_effect=[False, True]))
    resp = await _post(http, _callback_body(), delivery="dlv-race")
    assert resp.status == 200
    assert (await resp.json())["status"] == "duplicate"
    async with factory() as session:  # 冲突回滚:业务状态与去重记录都保持不变
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.DELEGATING
        count = (
            await session.execute(select(func.count()).select_from(UpstreamDelivery))
        ).scalar_one()
    assert count == 1


async def test_webhook_nonstring_txid_ignored(webhook_client) -> None:
    http, factory = webhook_client
    resp = await _post(http, _callback_body(txid=12345), delivery="dlv-tx")  # 防御:非法类型
    assert resp.status == 200
    assert (await resp.json())["status"] == "applied"
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.ACTIVE
        assert order.upstream_txid is None  # 非字符串 txid 不入库,但不阻断流转


async def test_webhook_rejects_oversized_body() -> None:
    app = _build_app(WEBHOOK_SECRET, session_factory=None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/upstream/tronow/webhook", data=b" " * (70 * 1024))
        assert resp.status == 413
    finally:
        await client.close()


async def test_webhook_503_when_secret_missing() -> None:
    app = _build_app("", session_factory=None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/upstream/tronow/webhook", data=b"{}")
        assert resp.status == 503
    finally:
        await client.close()


@pytest.mark.parametrize(
    "kind", ["missing_id", "unknown_status", "wrong_event", "wrong_body_event", "bad_data"]
)
async def test_invalid_callback_is_not_consumed(webhook_client, kind: str) -> None:
    http, factory = webhook_client
    payload = json.loads(_callback_body())
    event = "order.succeeded"
    if kind == "missing_id":
        del payload["data"]["order_id"]
    elif kind == "unknown_status":
        payload["data"]["status"] = "FUTURE_STATE"
    elif kind == "wrong_event":
        event = "order.failed"
    elif kind == "wrong_body_event":
        payload["event"] = "order.failed"
    else:
        payload["data"] = []
    response = await _post(http, json.dumps(payload).encode(), "invalid-dlv", event=event)
    assert response.status == 422
    async with factory() as session:
        assert await session.get(UpstreamDelivery, "invalid-dlv") is None
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.DELEGATING
    # 修正后的同一 delivery 可以重新处理,没有被错误去重。
    response = await _post(http, _callback_body(), "invalid-dlv")
    assert response.status == 200


@pytest.mark.parametrize("timestamp", ["NaN", "inf", "-inf", "1e9", "1700000000.0"])
async def test_invalid_timestamp_rejected_before_database(timestamp: str) -> None:
    app = _build_app(WEBHOOK_SECRET, session_factory=None)
    async with TestClient(TestServer(app)) as http:
        response = await _post(
            http,
            b"{}",
            "bad-ts",
            **{
                "X-Lease-Timestamp": timestamp,
                "X-Lease-Signature": _sign(
                    WEBHOOK_SECRET, timestamp, "bad-ts", "order.succeeded", b"{}"
                ),
            },
        )
        assert response.status == 401


async def test_signed_non_utf8_callback_is_bad_request() -> None:
    async with TestClient(TestServer(_build_app(WEBHOOK_SECRET, None))) as http:
        response = await _post(http, b"\xff", "bad-encoding")
        assert response.status == 400


async def test_callback_without_lease_field_activates(webhook_client) -> None:
    """业务不管理上游租期:回调缺少租期字段也能直接激活,不再需要查单补租期。"""
    http, factory = webhook_client
    payload = json.loads(_callback_body())
    del payload["data"]["lease_expires_at"]
    response = await _post(http, json.dumps(payload).encode(), "no-lease")
    assert response.status == 200
    async with factory() as session:
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.ACTIVE


@pytest.mark.parametrize("expiry", [123, "bad-time", "2026-09-20T12:00:00"])
async def test_lease_field_is_ignored(webhook_client, expiry: Any) -> None:
    """回调里的租期字段无论格式都被忽略,照常激活并确认投递。"""
    http, factory = webhook_client
    payload = json.loads(_callback_body())
    payload["data"]["lease_expires_at"] = expiry
    response = await _post(http, json.dumps(payload).encode(), "ignored-lease")
    assert response.status == 200
    async with factory() as session:
        assert await session.get(UpstreamDelivery, "ignored-lease") is not None
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.ACTIVE


async def test_concurrent_callbacks_apply_once(webhook_client) -> None:
    http, factory = webhook_client
    body = _callback_body()
    responses = await asyncio.gather(*(_post(http, body, "concurrent-dlv") for _ in range(4)))
    assert all(response.status == 200 for response in responses)
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(UpstreamDelivery))
        assert count == 1
        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id="ord_x1")
        assert order is not None and order.status is OrderStatus.ACTIVE


async def test_chunked_body_size_limit() -> None:
    async def chunks():
        for _ in range(9):
            yield b" " * 8192

    async with TestClient(TestServer(_build_app(WEBHOOK_SECRET, None))) as http:
        response = await http.post("/upstream/tronow/webhook", data=chunks())
        assert response.status == 413
