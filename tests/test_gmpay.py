"""GMPay 协议层测试:签名固定向量(与 epusdt sign_test.go 对照)+ 回环伪服务器。"""

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from energy_bot.config import GmpaySettings
from energy_bot.services.payment import gmpay
from energy_bot.services.payment.gmpay import GmpayApiError, GmpayClient

SECRET = "test-secret"
PID = "1000"
ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


# --- 签名固定向量(与 epusdt sign_test.go 逐一对照) ---


def test_sign_matches_epusdt_fixed_vector() -> None:
    # 与 TestGetHMACSHA256FixedVector 相同的输入与期望;跨语言实现一致性锚点
    params: dict[str, Any] = {
        "signature": "ignored",
        "pid": "1000",
        "empty": "",
        "nil": None,
        "name": "VIP",
        "amount": 100,
    }
    got = gmpay.sign_params(params, "test-secret")
    assert got == "ced9141fab53a83d1178f903e7c22d8a2a7033520f31e319934e92455a008b6c"


def test_canonical_params_rules() -> None:
    # ASCII 升序 + & 拼接
    assert gmpay.canonical_params({"b": "2", "a": "1"}) == "a=1&b=2"
    # 空串 / None / signature 剔除
    assert gmpay.canonical_params({"a": "", "b": None, "signature": "x", "c": "1"}) == "c=1"
    # 参数名区分大小写,ASCII 排序(大写在前)
    assert gmpay.canonical_params({"abc": "1", "Abc": "2"}) == "Abc=2&abc=1"
    # int 与 float 的规范化(对照 Go FormatFloat('f', -1, 64))
    assert gmpay.canonical_params({"n": 100}) == "n=100"
    assert gmpay.canonical_params({"n": 100.0}) == "n=100"  # 整数值浮点去尾零
    assert gmpay.canonical_params({"n": 14.2857}) == "n=14.2857"
    assert gmpay.canonical_params({"n": 0.05}) == "n=0.05"


def test_format_number_matches_go_format_float() -> None:
    # Go: strconv.FormatFloat(v, 'f', -1, 64) 的对照样例
    assert gmpay._format_number(100.0) == "100"
    assert gmpay._format_number(14.2857) == "14.2857"
    assert gmpay._format_number(0.1 + 0.2) == "0.30000000000000004"  # float 精确展开
    with pytest.raises(ValueError, match="范围"):
        gmpay._format_number(1e21)  # 科学计数法领域直接拒绝,不静默签错


def test_verify_callback_accepts_and_rejects() -> None:
    payload: dict[str, Any] = {
        "pid": PID,
        "trade_id": "tid-1",
        "order_id": "dep-1",
        "amount": 50.0,
        "actual_amount": 50.0,
        "token": "trx",
        "status": 2,
    }
    payload["signature"] = gmpay.sign_params(payload, SECRET)
    assert gmpay.verify_callback(payload, SECRET)
    # 篡改任一要素必须失败
    for key, bad in (("actual_amount", 50.01), ("trade_id", "tid-2"), ("status", 3)):
        tampered = {**payload, key: bad}
        assert not gmpay.verify_callback(tampered, SECRET), f"篡改 {key} 未被拒绝"
    assert not gmpay.verify_callback(payload, "other-secret")
    assert not gmpay.verify_callback({**payload, "signature": "v2=" + payload["signature"]}, SECRET)
    assert not gmpay.verify_callback({"a": 1}, SECRET)  # 缺 signature


# --- 回环伪服务器 ---


def _build_app(state: dict[str, Any]) -> web.Application:
    async def create(request: web.Request) -> web.Response:
        body = json.loads(await request.read())
        # 服务端独立重算签名(复刻网关验签逻辑)
        signature = body.pop("signature", "")
        assert gmpay.hmac.compare_digest(gmpay.sign_params(body, SECRET), signature), (
            "请求签名与服务端重算不一致"
        )
        assert body["pid"] == PID
        assert body["currency"] == "trx"
        assert body["token"] == "trx"
        assert body["network"] == "tron"
        assert body["amount"] == 50
        state["created"] = state.get("created", 0) + 1
        return web.json_response(
            {
                "code": 200,
                "msg": "ok",
                "data": {
                    "trade_id": "tid-abc",
                    "order_id": body["order_id"],
                    "amount": 50,
                    "currency": "trx",
                    "actual_amount": 50,
                    "receive_address": ADDRESS,
                    "token": "trx",
                    "status": 1,
                    "expiration_time": 1800000000,
                    "payment_url": "https://pay.example.com/cashier/tid-abc",
                },
            }
        )

    async def check_status(request: web.Request) -> web.Response:
        mode = state.get("mode", "ok")
        if mode == "biz_error":
            return web.json_response({"code": 10004, "msg": "bad amount"}, status=400)
        if mode == "html":
            return web.Response(text="<html>gateway</html>", content_type="text/html", status=502)
        return web.json_response({"code": 200, "msg": "ok", "data": {"status": 2}})

    app = web.Application()
    app.router.add_post("/payments/gmpay/v1/order/create-transaction", create)
    app.router.add_get("/pay/check-status/{trade_id}", check_status)
    return app


@pytest.fixture
async def fake() -> AsyncIterator[tuple[TestClient, dict[str, Any]]]:
    state: dict[str, Any] = {}
    client = TestClient(TestServer(_build_app(state)))
    await client.start_server()
    yield client, state
    await client.close()


def _settings(base: str) -> GmpaySettings:
    return GmpaySettings(base_url=base, pid=PID, secret_key=SECRET)


async def test_create_transaction_roundtrip(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with GmpayClient(_settings(str(http.make_url("")))) as client:
        tx = await client.create_transaction(
            order_id="dep-1-abcdef0123",
            amount=Decimal("50"),
            notify_url="https://bot.example.com/payment/gmpay/notify",
        )
    assert tx.trade_id == "tid-abc"
    assert tx.actual_amount == Decimal("50")
    assert tx.receive_address == ADDRESS
    assert tx.status == 1


async def test_check_status_roundtrip(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with GmpayClient(_settings(str(http.make_url("")))) as client:
        assert await client.check_status("tid-abc") == 2


async def test_business_error_passthrough(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, state = fake
    state["mode"] = "biz_error"
    async with GmpayClient(_settings(str(http.make_url("")))) as client:
        with pytest.raises(GmpayApiError) as excinfo:
            await client.check_status("tid-abc")
    assert excinfo.value.code == "10004"
    assert excinfo.value.status == 400
    assert excinfo.value.args[0] == "bad amount"


async def test_html_error_is_http_error(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, state = fake
    state["mode"] = "html"
    async with GmpayClient(_settings(str(http.make_url("")))) as client:
        with pytest.raises(GmpayApiError) as excinfo:
            await client.check_status("tid-abc")
    assert excinfo.value.code == "HTTP_ERROR"
    assert excinfo.value.status == 502


# --- 客户端侧防线 ---


async def test_long_order_id_rejected() -> None:
    async with GmpayClient(GmpaySettings(base_url="https://x", secret_key=SECRET)) as client:
        with pytest.raises(ValueError, match="order_id"):
            await client.create_transaction(
                order_id="x" * 33, amount=Decimal("1"), notify_url="https://x/notify"
            )


def test_non_loopback_http_base_rejected() -> None:
    client = GmpayClient(GmpaySettings(base_url="http://pay.example.com", secret_key=SECRET))
    with pytest.raises(ValueError, match="https"):
        client._base()


def test_amount_validation() -> None:
    assert gmpay._amount(50, "f") == Decimal("50")
    assert gmpay._amount(14.2857, "f") == Decimal("14.2857")
    assert gmpay._amount("50.5", "f") == Decimal("50.5")
    for bad in ("1e3", "-1", "abc", "", None, True, "NaN"):
        with pytest.raises(GmpayApiError, match="INVALID_RESPONSE"):
            gmpay._amount(bad, "f")
