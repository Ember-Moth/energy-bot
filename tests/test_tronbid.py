"""TronBid 客户端测试:回环伪服务器全流程 + 客户端侧防线。

伪服务器校验 Authorization 头并按 idempotency_key 模拟幂等(同键同载荷
重放返回 duplicate=true,同键异载荷返回 409),金额字段用 Decimal 断言。
"""

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from energy_bot.config import Settings, TronbidSettings
from energy_bot.services.upstream.tronbid import (
    TronbidApiError,
    TronbidClient,
    TronbidOrderStatus,
    _trx,
)

API_KEY = "test-key"
ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
PAYER = "TA4Y62o6YC2Zsck9rZVGTvqW1AQ7X9zTnj"
ORDER_ID = "3f6a2a90-9d35-4f2a-9e1c-2b2b7f0a0a01"

ORDER_PAYLOAD: dict[str, Any] = {
    "id": ORDER_ID,
    "status": "pending_payment",
    "payment_mode": "onchain",
    "amount_trx": "3.200000",
    "energy_amount": 131000,
    "effective_energy_amount": None,
    "duration_minutes": 15,
    "pay_address": PAYER,
    "payer_address": PAYER,
    "target_address": ADDRESS,
    "expires_at": "2026-09-20T12:30:00+08:00",
    "qr_payload": None,
    "error_code": None,
    "error_message": None,
}

CREATE_PAYLOAD: dict[str, Any] = {
    "idempotency_key": "eb-onchain-001",
    "target_address": ADDRESS,
    "energy_amount": 131000,
    "duration_minutes": 15,
    "payment_mode": "onchain",
    "payer_address": PAYER,
}


def _err(code: str, status: int, **kwargs: Any) -> web.Response:
    return web.json_response({"error": code, "message": f"err {code}"}, status=status, **kwargs)


def _build_app(state: dict[str, Any]) -> web.Application:
    async def verify(request: web.Request) -> None:
        assert request.headers.get("Authorization") == f"Bearer {API_KEY}"

    async def quote(request: web.Request) -> web.Response:
        await verify(request)
        body = json.loads(await request.read())
        assert body == {"energy_amount": 131000, "duration_minutes": 15}
        if state.get("mode") == "unavailable":
            return _err("quote_unavailable", 400)
        return web.json_response(
            {"price_trx": "3.200000", "available": True, "save_percent": 62, "expires_in_sec": 30}
        )

    async def create_order(request: web.Request) -> web.Response:
        await verify(request)
        body = json.loads(await request.read())
        # 冲突用例的同键异载荷由下方幂等逻辑处理,这里只校验公共字段
        assert body["idempotency_key"] == CREATE_PAYLOAD["idempotency_key"]
        assert body["target_address"] == CREATE_PAYLOAD["target_address"]
        seen: dict[str, bytes] = state.setdefault("orders", {})
        raw = json.dumps(body, sort_keys=True).encode()
        key = body["idempotency_key"]
        if key in seen:
            if seen[key] != raw:
                return _err("idempotency_conflict", 409)
            return web.json_response({**ORDER_PAYLOAD, "duplicate": True})
        seen[key] = raw
        return web.json_response(ORDER_PAYLOAD)

    async def get_order(request: web.Request) -> web.Response:
        await verify(request)
        status = state.get("order_status", "delegated")
        if status == "missing":
            return _err("order_not_found", 404)
        return web.json_response(
            {**ORDER_PAYLOAD, "status": status, "effective_energy_amount": 131000}
        )

    async def cancel(request: web.Request) -> web.Response:
        await verify(request)
        if state.get("mode") == "already_paid":
            return _err("order_not_cancellable", 409)
        return web.json_response({**ORDER_PAYLOAD, "status": "cancelled"})

    async def set_payer(request: web.Request) -> web.Response:
        await verify(request)
        body = json.loads(await request.read())
        assert body == {"payer_address": ADDRESS}
        return web.json_response({**ORDER_PAYLOAD, "payer_address": ADDRESS})

    async def balance(request: web.Request) -> web.Response:
        await verify(request)
        mode = state.get("mode", "ok")
        if mode == "unauthorized":
            return _err("invalid_api_key", 401)
        if mode == "rate_limited":
            return _err("rate_limited", 429, headers={"Retry-After": "3"})
        if mode == "html":
            return web.Response(text="<html>gateway</html>", content_type="text/html", status=502)
        if mode == "bad_payload":
            return web.json_response({"balance_trx": "not-a-number"})
        return web.json_response({"balance_trx": "150.500000"})

    app = web.Application()
    app.router.add_post("/api/v2/quick-rent/quote", quote)
    app.router.add_post("/api/v2/quick-rent/orders", create_order)
    app.router.add_get("/api/v2/quick-rent/orders/{id}", get_order)
    app.router.add_post("/api/v2/quick-rent/orders/{id}/cancel", cancel)
    app.router.add_post("/api/v2/quick-rent/orders/{id}/set-payer", set_payer)
    app.router.add_get("/api/v2/quick-rent/balance", balance)
    return app


@pytest.fixture
async def fake() -> AsyncIterator[tuple[TestClient, dict[str, Any]]]:
    state: dict[str, Any] = {}
    client = TestClient(TestServer(_build_app(state)))
    await client.start_server()
    yield client, state
    await client.close()


def _settings(base: str) -> TronbidSettings:
    return TronbidSettings(base_url=base, api_key=API_KEY)


# --- 回环伪服务器全流程 ---


async def test_quote_roundtrip(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        quote = await client.create_quote(energy_amount=131000, duration_minutes=15)
    assert quote.price_trx == Decimal("3.200000")
    assert quote.available is True
    assert quote.save_percent == 62.0
    assert quote.expires_in_sec == 30


async def test_create_order_and_idempotent_replay(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        first = await client.create_order(
            idempotency_key="eb-onchain-001",
            target_address=ADDRESS,
            energy_amount=131000,
            duration_minutes=15,
            payer_address=PAYER,
        )
        assert first.id == ORDER_ID
        assert first.status is TronbidOrderStatus.PENDING_PAYMENT
        assert first.amount_trx == Decimal("3.200000")
        assert first.duplicate is False
        second = await client.create_order(
            idempotency_key="eb-onchain-001",
            target_address=ADDRESS,
            energy_amount=131000,
            duration_minutes=15,
            payer_address=PAYER,
        )
        assert second.id == ORDER_ID
        assert second.duplicate is True  # 幂等重放


async def test_idempotency_conflict_surfaces_409(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        await client.create_order(
            idempotency_key="eb-onchain-001",
            target_address=ADDRESS,
            energy_amount=131000,
            duration_minutes=15,
            payer_address=PAYER,
        )
        with pytest.raises(TronbidApiError) as excinfo:  # 同键不同载荷
            await client.create_order(
                idempotency_key="eb-onchain-001",
                target_address=ADDRESS,
                energy_amount=65000,
                duration_minutes=15,
                payer_address=PAYER,
            )
    assert excinfo.value.code == "idempotency_conflict"
    assert excinfo.value.status == 409


async def test_get_order_roundtrip(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        order = await client.get_order(ORDER_ID)
    assert order.status is TronbidOrderStatus.DELEGATED
    assert order.effective_energy_amount == 131000


async def test_cancel_and_set_payer(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        cancelled = await client.cancel_order(ORDER_ID)
        assert cancelled.status is TronbidOrderStatus.CANCELLED
        updated = await client.set_order_payer(ORDER_ID, payer_address=ADDRESS)
        assert updated.payer_address == ADDRESS


async def test_balance_roundtrip(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, _ = fake
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        balance = await client.get_balance()
    assert balance.balance_trx == Decimal("150.500000")


# --- 错误与防御分支 ---


async def test_error_code_and_message_passthrough(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, state = fake
    state["mode"] = "unauthorized"
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        with pytest.raises(TronbidApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "invalid_api_key"
    assert excinfo.value.status == 401
    assert excinfo.value.args[0] == "err invalid_api_key"


async def test_rate_limit_carries_retry_after(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, state = fake
    state["mode"] = "rate_limited"
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        with pytest.raises(TronbidApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "rate_limited"
    assert excinfo.value.retry_after == 3


async def test_html_error_is_http_error(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, state = fake
    state["mode"] = "html"
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        with pytest.raises(TronbidApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "HTTP_ERROR"
    assert excinfo.value.status == 502


async def test_bad_amount_is_invalid_response(fake: tuple[TestClient, dict[str, Any]]) -> None:
    http, state = fake
    state["mode"] = "bad_payload"
    async with TronbidClient(_settings(str(http.make_url("/api/v2/quick-rent")))) as client:
        with pytest.raises(TronbidApiError) as excinfo:
            await client.get_balance()
    assert excinfo.value.code == "INVALID_RESPONSE"


async def test_short_idempotency_key_rejected() -> None:
    async with TronbidClient(TronbidSettings(api_key=API_KEY)) as client:
        with pytest.raises(ValueError, match="idempotency_key"):
            await client.create_order(
                idempotency_key="short",
                target_address=ADDRESS,
                energy_amount=65000,
                duration_minutes=15,
            )


async def test_get_must_not_carry_body() -> None:
    async with TronbidClient(TronbidSettings(api_key=API_KEY)) as client:
        with pytest.raises(ValueError, match="body"):
            await client._request("GET", "balance", data={"a": 1})


def test_non_loopback_http_base_rejected() -> None:
    client = TronbidClient(TronbidSettings(base_url="http://tronbid.example.com/api/v2/quick-rent"))
    with pytest.raises(ValueError, match="https"):
        client._base()


def test_trx_amount_validation() -> None:
    assert _trx("3.200000", "f") == Decimal("3.200000")
    assert _trx("0", "f") == Decimal("0")
    for bad in ("1e3", "1e-3", "10e0", "+1", " 1", "-1", "abc", "", None, 3.2, "NaN", "Infinity"):
        with pytest.raises(TronbidApiError, match="INVALID_RESPONSE"):
            _trx(bad, "f")


def test_tronbid_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_BOT_UPSTREAM__TRONBID__API_KEY", "from-env")
    settings = Settings()
    assert settings.upstream.tronbid.api_key == "from-env"
    assert settings.upstream.tronbid.base_url == "https://tronbid.com/api/v2/quick-rent"
    assert settings.upstream.tronbid.timeout_seconds == 10.0
