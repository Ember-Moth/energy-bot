"""TRONow 客户端测试:签名固定向量 + 回环伪服务器全流程。

伪服务器用独立重算的签名校验每个请求(方法/路径/查询/body 字节与
请求头一致),因此能抓住"签的内容与发的内容不一致"这类错位。
"""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from energy_bot.config import Settings, TronowSettings
from energy_bot.services.upstream import tronow
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient, TronowOrderStatus

API_KEY = "test-key"
API_SECRET = "test-secret"
ADDRESS = "T" + "2" * 33

BALANCE_PAYLOAD = {
    "currency": "TRX",
    "available_balance_sun": "100000000",
    "reserved_balance_sun": "1500000",
    "total_balance_sun": "101500000",
    "updated_at": "2026-09-20T12:00:00+08:00",
}

ORDER_PAYLOAD: dict[str, Any] = {
    "order_id": "ord_abc123",
    "client_order_id": "eb-000001",
    "status": "PROCESSING",
    "resource_type": "ENERGY",
    "receiver_address": ADDRESS,
    "resource_amount": 65000,
    "duration": "1h",
    "amount_sun": "3250000",
    "currency": "TRX",
    "txid": None,
    "failure_code": None,
    "created_at": "2026-09-20T12:00:00+08:00",
    "confirmed_at": None,
    "lease_expires_at": None,
}


def _ok(data: Any, **kwargs: Any) -> web.Response:
    return web.json_response(
        {"code": "OK", "message": "success", "request_id": "req_1", "data": data}, **kwargs
    )


def _err(code: str, status: int, **kwargs: Any) -> web.Response:
    return web.json_response(
        {"code": code, "message": "err", "request_id": "req_1"}, status=status, **kwargs
    )


def _build_app(state: dict[str, str]) -> web.Application:
    async def verify(request: web.Request) -> bytes:
        body = await request.read()
        expected = tronow.sign(
            API_SECRET,
            method=request.method,
            path=request.path,
            query=request.query_string,
            timestamp=request.headers["X-Timestamp"],
            nonce=request.headers["X-Nonce"],
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            body=body,
        )
        assert hmac.compare_digest(expected, request.headers["X-Signature"]), (
            "签名与服务端重算不一致"
        )
        assert request.headers["X-API-Key"] == API_KEY
        return body

    async def balance(request: web.Request) -> web.Response:
        await verify(request)
        mode = state.get("mode", "ok")
        if mode == "biz_error":
            return _err("INSUFFICIENT_BALANCE", 422)
        if mode == "rate_limited":
            return _err("RATE_LIMITED", 429, headers={"Retry-After": "5"})
        if mode == "html":
            return web.Response(text="<html>gateway</html>", content_type="text/html")
        if mode == "bad_gateway":
            return web.Response(status=502, body=b"")
        return _ok(BALANCE_PAYLOAD)

    async def quote(request: web.Request) -> web.Response:
        await verify(request)
        assert request.query["resource_type"] == "ENERGY"
        assert request.query["resource_amount"] == "65000"
        assert request.query["duration"] == "1h"
        return _ok(
            {
                "resource_type": "ENERGY",
                "resource_amount": 65000,
                "duration": "1h",
                "price_sun": "3250000",
                "currency": "TRX",
                "priced_at": "2026-09-20T12:00:00+08:00",
            }
        )

    async def create_order(request: web.Request) -> web.Response:
        body = await verify(request)
        assert json.loads(body) == {
            "client_order_id": "eb-000001",
            "resource_type": "ENERGY",
            "receiver_address": ADDRESS,
            "resource_amount": 65000,
            "duration": "1h",
        }
        assert request.headers["Idempotency-Key"] == "eb-000001"
        state["created"] = str(int(state.get("created", "0")) + 1)
        status = 201 if state["created"] == "1" else 200  # 首次受理 / 幂等重放
        return _ok(
            {
                "order_id": "ord_abc123",
                "client_order_id": "eb-000001",
                "status": "PROCESSING",
                "reserved_amount_sun": "3250000",
                "currency": "TRX",
                "created_at": "2026-09-20T12:00:00+08:00",
            },
            status=status,
            headers={"Location": "/openapi/v1/orders/ord_abc123", "Retry-After": "2"},
        )

    async def get_order(request: web.Request) -> web.Response:
        await verify(request)
        return _ok(ORDER_PAYLOAD)

    async def get_by_client(request: web.Request) -> web.Response:
        await verify(request)
        assert request.query["client_order_id"] == "eb-000001"
        return _ok(ORDER_PAYLOAD)

    app = web.Application()
    app.router.add_get("/openapi/v1/account/balance", balance)
    app.router.add_get("/openapi/v1/prices/quote", quote)
    app.router.add_post("/openapi/v1/orders", create_order)
    app.router.add_get("/openapi/v1/orders", get_by_client)
    app.router.add_get("/openapi/v1/orders/{order_id}", get_order)
    return app


@pytest.fixture
async def fake() -> AsyncIterator[tuple[TestClient, dict[str, str]]]:
    state: dict[str, str] = {}
    client = TestClient(TestServer(_build_app(state)))
    await client.start_server()
    yield client, state
    await client.close()


def _settings(base: str) -> TronowSettings:
    return TronowSettings(base_url=base, api_key=API_KEY, api_secret=API_SECRET)


# --- 签名固定向量(独立于实现手工构造规范串) ---


def test_canonical_query_vectors() -> None:
    assert tronow.canonical_query("") == ""
    assert tronow.canonical_query("b=2&a=1") == "a=1&b=2"  # 键排序
    assert tronow.canonical_query("a=2&a=1") == "a=2&a=1"  # 同键保持原值顺序
    assert tronow.canonical_query("b=2&a=1&a=0") == "a=1&a=0&b=2"
    assert tronow.canonical_query("q=hello world") == "q=hello+world"  # 空格转 +
    assert tronow.canonical_query("q=hello%20world") == "q=hello+world"
    assert tronow.canonical_query("q=%E4%BD%A0%E5%A5%BD") == "q=%E4%BD%A0%E5%A5%BD"  # UTF-8
    assert tronow.canonical_query("k=a!b'c(d)e*f") == "k=a%21b%27c%28d%29e%2Af"  # RFC3986 严格转义
    assert tronow.canonical_query("empty=") == "empty="  # 空值保留


def test_sign_fixed_vector() -> None:
    body = b'{"a":1}'
    canonical = "\n".join(
        [
            "POST",
            "/openapi/v1/orders",
            "",
            "1700000000000",
            "abcdef0123456789",
            "idem-key-1",
            hashlib.sha256(body).hexdigest(),
        ]
    )
    expected = hmac.new(b"s3cr3t", canonical.encode(), hashlib.sha256).hexdigest()
    # method 归一化为大写;读操作幂等键为空行由空串自然形成
    got = tronow.sign(
        "s3cr3t",
        method="post",
        path="/openapi/v1/orders",
        query="",
        timestamp="1700000000000",
        nonce="abcdef0123456789",
        idempotency_key="idem-key-1",
        body=body,
    )
    assert got == expected


# --- 回环伪服务器全流程 ---


async def test_balance_roundtrip(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, _ = fake
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        balance = await client.get_balance()
    assert balance.available_balance_sun == 100_000_000
    assert balance.reserved_balance_sun == 1_500_000
    assert balance.total_balance_sun == 101_500_000


async def test_quote_roundtrip(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, _ = fake
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        quote = await client.get_quote(65000)
    assert quote.price_sun == 3_250_000
    assert quote.duration == "1h"


async def test_create_order_and_idempotent_replay(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, state = fake
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        first = await client.create_order(
            client_order_id="eb-000001", receiver_address=ADDRESS, resource_amount=65000
        )
        assert first.accepted.order_id == "ord_abc123"
        assert first.accepted.reserved_amount_sun == 3_250_000
        assert first.accepted.status is TronowOrderStatus.PROCESSING
        assert first.retry_after == 2
        second = await client.create_order(
            client_order_id="eb-000001", receiver_address=ADDRESS, resource_amount=65000
        )
        assert second.accepted.order_id == "ord_abc123"
    assert state["created"] == "2"


async def test_get_order_roundtrip(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, _ = fake
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        order = await client.get_order("ord_abc123")
        assert order.status is TronowOrderStatus.PROCESSING
        assert order.amount_sun == 3_250_000
        by_client = await client.get_order_by_client_id("eb-000001")
    assert by_client.order_id == "ord_abc123"
    assert by_client.txid is None


async def test_business_error(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, state = fake
    state["mode"] = "biz_error"
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        with pytest.raises(TronowApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "INSUFFICIENT_BALANCE"
    assert excinfo.value.status == 422
    assert excinfo.value.request_id == "req_1"


async def test_rate_limit_carries_retry_after(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, state = fake
    state["mode"] = "rate_limited"
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        with pytest.raises(TronowApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "RATE_LIMITED"
    assert excinfo.value.retry_after == 5


async def test_html_response_is_invalid_response(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, state = fake
    state["mode"] = "html"
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        with pytest.raises(TronowApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "INVALID_RESPONSE"


async def test_bad_gateway_is_http_error(fake: tuple[TestClient, dict[str, str]]) -> None:
    http, state = fake
    state["mode"] = "bad_gateway"
    async with TronowClient(_settings(str(http.make_url("/openapi/v1")))) as client:
        with pytest.raises(TronowApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "HTTP_ERROR"


# --- 客户端侧防线 ---


async def test_long_client_order_id_rejected() -> None:
    async with TronowClient(TronowSettings(api_key=API_KEY, api_secret=API_SECRET)) as client:
        with pytest.raises(ValueError, match="client_order_id"):
            await client.create_order(
                client_order_id="x" * 65, receiver_address=ADDRESS, resource_amount=65000
            )


async def test_get_must_not_carry_body() -> None:
    async with TronowClient(TronowSettings(api_key=API_KEY, api_secret=API_SECRET)) as client:
        with pytest.raises(ValueError, match="body"):
            await client._request("GET", "orders", data={"a": 1})


def test_non_loopback_http_base_rejected() -> None:
    client = TronowClient(TronowSettings(base_url="http://api.example.com/openapi/v1"))
    with pytest.raises(ValueError, match="https"):
        client._base()


def test_upstream_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_BOT_UPSTREAM__TRONOW__API_KEY", "from-env")
    settings = Settings()
    assert settings.upstream.tronow.api_key == "from-env"
    assert settings.upstream.tronow.base_url == "https://api.tronow.io/openapi/v1"
    assert settings.upstream.tronow.timeout_seconds == 10.0


@pytest.mark.parametrize("value", ["²", "１２", "1e3", "-1", "1.0"])
def test_sun_rejects_non_ascii_integer(value: str) -> None:
    with pytest.raises(TronowApiError, match="INVALID_RESPONSE"):
        tronow._sun(value, "price_sun")
