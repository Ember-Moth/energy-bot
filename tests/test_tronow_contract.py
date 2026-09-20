"""用户提供的 TRONow 商户限流和错误语义回归。"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from energy_bot.config import TIMEZONE, RentalSettings, Settings, TronowSettings, UpstreamSettings
from energy_bot.models import Order, PurchaseAttempt, UpstreamThrottle
from energy_bot.services.procurement import OrderWorker, _definite_rejection
from energy_bot.services.providers import TronowProvider, build_providers
from energy_bot.services.upstream.metadata import RateLimitSnapshot
from energy_bot.services.upstream.tronow import (
    CreatedOrder,
    TronowApiError,
    TronowBalance,
    TronowClient,
    TronowOrder,
    TronowOrderAccepted,
    TronowOrderStatus,
    TronowQuote,
)
from energy_bot.services.upstream_gate import TronowGate
from test_order_system import ADDRESS, FakeProvider, assert_money, due, funded, reserve


async def test_rolling_window_shares_both_budgets(db_factory):
    settings = RentalSettings()
    a = TronowGate(db_factory, "merchant", settings, request_limit=4, order_limit=2)
    b = TronowGate(db_factory, "merchant", settings, request_limit=4, order_limit=2)
    assert await a._delay(True) == 0
    assert await b._delay(True) == 0
    assert await a._delay(True) > 0
    assert await b._delay(False) == 0
    assert await a._delay(False) == 0
    assert await b._delay(False) > 0
    async with db_factory() as session, session.begin():
        row = await session.get(UpstreamThrottle, "merchant")
        now = await session.scalar(select(func.clock_timestamp()))
        row.request_history = [now - timedelta(seconds=1.1), now - timedelta(seconds=0.5)]
        row.order_history = []
    small = TronowGate(db_factory, "merchant", settings, request_limit=2, order_limit=2)
    assert await small._delay(False) == 0
    assert 0 < await small._delay(False) < 0.7


async def test_snapshot_limits_cap_but_remaining_does_not_mint_tokens(db_factory):
    gate = TronowGate(db_factory, "merchant", RentalSettings(), request_limit=5, order_limit=3)
    await gate.observe(
        RateLimitSnapshot(
            request_limit=2, request_remaining=999, order_limit=1, order_remaining=999
        )
    )
    assert await gate._delay(True) == 0
    assert await gate._delay(True) > 0
    assert await gate._delay(False) == 0
    assert await gate._delay(False) > 0
    await gate.observe(RateLimitSnapshot(request_remaining=999, order_remaining=999))
    assert await gate._delay(False) > 0
    async with db_factory() as session:
        row = await session.get(UpstreamThrottle, "merchant")
        assert row.rate_snapshot["request_limit"] == 2  # 部分响应头不覆盖已知上限
        assert len(row.request_history) == 2


@pytest.mark.parametrize(
    "status,code,scope,reads_blocked",
    [
        (429, "RATE_LIMITED", "orders", False),
        (429, "RATE_LIMITED", "requests", True),
        (503, "RATE_LIMIT_UNAVAILABLE", "orders", True),
        (429, "HTTP_ERROR", "orders", True),
    ],
)
async def test_scope_specific_retry_after(db_factory, status, code, scope, reads_blocked):
    a, b = (TronowGate(db_factory, "merchant", RentalSettings()) for _ in range(2))
    request = AsyncMock(
        side_effect=TronowApiError(
            code, status, retry_after=120, rate_limits=RateLimitSnapshot(scope=scope)
        )
    )
    with pytest.raises(TronowApiError):
        await a.run(request, creation=True)
    assert await b._delay(True) >= 119
    assert (await b._delay(False) > 0) == reads_blocked


async def test_default_scope_shares_different_api_keys(db_factory):
    groups = [
        build_providers(
            UpstreamSettings(
                tronow=TronowSettings(
                    api_key=key,
                    api_secret="synthetic",
                    base_url=f"https://{key}.example/openapi/v1",
                )
            ),
            db_factory,
            RentalSettings(),
        )
        for key in ("key-a", "key-b")
    ]
    gates = [
        cast(TronowGate, cast(TronowProvider, group["tronow"]).client._gate) for group in groups
    ]
    assert all(isinstance(gate, TronowGate) for gate in gates)
    assert gates[0].scope == gates[1].scope
    for group in groups:
        await group["tronow"].close()


async def test_order_activation_and_replay_consume_order_budget(db_factory):
    async def response(request):
        return web.json_response({"code": "OK", "data": {}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", response)
    gate = TronowGate(db_factory, "merchant", RentalSettings(), request_limit=10, order_limit=3)
    async with TestClient(TestServer(app)) as http:
        async with TronowClient(
            TronowSettings(base_url=str(http.make_url("/openapi/v1"))), gate=gate
        ) as c:
            for resource in ("orders", "orders", "address-activations"):
                await c._request("POST", resource, data={}, idempotency_key="same-request-key")
            await c._request("GET", "orders/ord_test")
    async with db_factory() as session:
        row = await session.get(UpstreamThrottle, "merchant")
        assert len(row.order_history) == 3
        assert len(row.request_history) == 4


@pytest.mark.parametrize(
    "status,payload,expected,raw_code",
    [
        (429, "<html>gateway</html>", "HTTP_ERROR", None),
        (502, b"", "HTTP_ERROR", None),
        (504, b"\xff", "HTTP_ERROR", None),
        (200, "<html>gateway</html>", "INVALID_RESPONSE", None),
        (
            503,
            {"code": "RATE_LIMIT_UNAVAILABLE"},
            "RATE_LIMIT_UNAVAILABLE",
            "RATE_LIMIT_UNAVAILABLE",
        ),
        (500, {"code": "FUTURE_FAILURE"}, "FUTURE_FAILURE", "FUTURE_FAILURE"),
        (500, {"code": "future-code-v2"}, "HTTP_ERROR", "future-code-v2"),
        (200, {"code": "OK", "data": {}}, "INVALID_RESPONSE", "OK"),
    ],
)
async def test_error_metadata_survives_json_and_gateway_failures(
    status, payload, expected, raw_code
):
    async def respond(request):
        headers = {
            "X-Request-ID": "req-synthetic",
            "Retry-After": "2",
            "X-RateLimit-Limit": "50",
            "X-RateLimit-Remaining": "0",
            "X-OrderRateLimit-Limit": "10",
            "X-OrderRateLimit-Remaining": "0",
            "X-RateLimit-Scope": "orders",
        }
        if isinstance(payload, dict):
            return web.json_response(payload, status=status, headers=headers)
        return web.Response(status=status, body=payload, headers=headers)

    app = web.Application()
    app.router.add_get("/openapi/v1/account/balance", respond)
    async with TestClient(TestServer(app)) as http:
        async with TronowClient(TronowSettings(base_url=str(http.make_url("/openapi/v1")))) as c:
            with pytest.raises(TronowApiError) as caught:
                await c.get_balance()
    error = caught.value
    assert (error.code, error.raw_code, error.status) == (expected, raw_code, status)
    assert error.request_id == "req-synthetic" and error.retry_after == 2
    assert error.rate_limits == RateLimitSnapshot(50, 0, 10, 0, "orders")


async def test_body_read_failure_retains_headers():
    async def respond(request):
        response = web.StreamResponse(
            status=502, headers={"X-Request-ID": "req-stream", "Retry-After": "2"}
        )
        await response.prepare(request)
        await response.write(b'{"code":')
        await asyncio.sleep(0.15)
        return response

    app = web.Application()
    app.router.add_get("/openapi/v1/account/balance", respond)
    async with TestClient(TestServer(app)) as http:
        async with TronowClient(
            TronowSettings(base_url=str(http.make_url("/openapi/v1")), timeout_seconds=0.05)
        ) as c:
            with pytest.raises(TronowApiError) as caught:
                await c.get_balance()
    assert caught.value.code == "HTTP_ERROR"
    assert caught.value.status == 502 and caught.value.request_id == "req-stream"


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("INSUFFICIENT_BALANCE", 422, True),
        ("INSUFFICIENT_BALANCE", 503, False),
        ("INVALID_ARGUMENT", 400, True),
        ("INVALID_ARGUMENT", 502, False),
        ("RATE_LIMITED", 429, False),
        ("RATE_LIMIT_UNAVAILABLE", 503, False),
        ("FUTURE_FAILURE", 400, False),
    ],
)
def test_definite_failure_requires_matching_http_and_code(code, status, expected):
    assert _definite_rejection(TronowApiError(code, status)) is expected


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "RATE_LIMITED"),
        (503, "RATE_LIMIT_UNAVAILABLE"),
        (502, "HTTP_ERROR"),
        (503, "INSUFFICIENT_BALANCE"),
    ],
)
async def test_http_throttling_does_not_fail_paid_business(db_factory, status, code):
    await funded(db_factory)
    await reserve(db_factory)
    provider = FakeProvider()
    provider.submit = AsyncMock(side_effect=TronowApiError(code, status, "req-record", 2))
    await OrderWorker(db_factory, {provider.name: provider}, RentalSettings(batch_size=1)).tick()
    await assert_money(db_factory, "6", "4")
    async with db_factory() as session:
        attempt = await session.scalar(select(PurchaseAttempt))
        assert attempt.state == "submitting"
        assert attempt.last_http_status == status and attempt.last_request_id == "req-record"
        assert attempt.error_code == code


async def test_bounded_resubmission_keeps_funds_and_can_still_recover(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    client = AsyncMock(spec=TronowClient)
    client.get_quote.return_value = TronowQuote(65000, "1h", 2_000_000, "TRX", "now")
    client.get_balance.return_value = TronowBalance("TRX", 10_000_000, 0, 10_000_000, "now")
    client.submit_order.side_effect = TronowApiError("HTTP_ERROR", 502, "req-uncertain")
    client.get_order_by_client_id.side_effect = TronowApiError("ORDER_NOT_FOUND", 404, "req-lookup")
    service = OrderWorker(
        db_factory,
        {"tronow": TronowProvider(client)},
        RentalSettings(batch_size=1, max_submit_attempts=2),
    )
    for _ in range(4):
        await due(db_factory, order_id)
        await service.tick()
    assert client.submit_order.await_count == 2
    await assert_money(db_factory, "6", "4")
    async with db_factory() as session:
        attempt = await session.scalar(select(PurchaseAttempt))
        assert attempt.submit_attempts == 2 and attempt.state == "reviewing"
        business_id = attempt.business_id
    client.get_order_by_client_id.side_effect = None
    client.get_order_by_client_id.return_value = TronowOrder(
        order_id="ord_found",
        client_order_id=business_id,
        status=TronowOrderStatus.SUCCESS,
        receiver_address=ADDRESS,
        resource_amount=65000,
        duration="1h",
        amount_sun=2_000_000,
        currency="TRX",
        txid="synthetic",
        failure_code=None,
        created_at="now",
        confirmed_at=None,
        lease_expires_at=(datetime.now(TIMEZONE) + timedelta(minutes=30)).isoformat(),
    )
    await due(db_factory, order_id)
    await service.tick()
    assert client.submit_order.await_count == 2
    await assert_money(db_factory, "6", "0", captures=1)


async def test_disconnected_request_has_client_fallback(db_factory):
    nonces = []

    async def disconnect(request):
        nonces.append(request.headers["X-Nonce"])
        request.transport.close()
        return web.Response()

    app = web.Application()
    app.router.add_get("/openapi/v1/account/balance", disconnect)
    async with TestClient(TestServer(app)) as http:
        async with TronowClient(
            TronowSettings(base_url=str(http.make_url("/openapi/v1"))),
            gate=TronowGate(db_factory, "disconnected", RentalSettings()),
        ) as c:
            for _ in range(2):
                with pytest.raises(TronowApiError) as caught:
                    await c.get_balance()
                assert caught.value.code == "HTTP_ERROR" and caught.value.status is None
    assert len(nonces) == len(set(nonces)) == 2
    async with db_factory() as session:
        row = await session.get(UpstreamThrottle, "disconnected")
        assert len(row.request_history) == 2


async def test_delivery_failure_is_successful_http_query():
    async def result(request):
        return web.json_response(
            {
                "code": "OK",
                "request_id": "req-query",
                "data": {
                    "order_id": "ord_test",
                    "client_order_id": "eb-test",
                    "status": "FAILED",
                    "receiver_address": ADDRESS,
                    "resource_amount": 65000,
                    "duration": "1h",
                    "amount_sun": "2000000",
                    "currency": "TRX",
                    "txid": None,
                    "failure_code": "DELIVERY_REJECTED",
                    "created_at": "now",
                },
            }
        )

    app = web.Application()
    app.router.add_get("/openapi/v1/orders/ord_test", result)
    async with TestClient(TestServer(app)) as http:
        async with TronowClient(TronowSettings(base_url=str(http.make_url("/openapi/v1")))) as c:
            order = await c.get_order("ord_test")
    assert order.status is TronowOrderStatus.FAILED
    assert order.failure_code == "DELIVERY_REJECTED"


async def test_only_http_404_not_found_can_authorize_resubmission():
    client = AsyncMock(spec=TronowClient)
    client.get_order_by_client_id.side_effect = TronowApiError("ORDER_NOT_FOUND", 503)
    attempt = PurchaseAttempt(business_id="same-business", state="submitting")
    with pytest.raises(TronowApiError):
        await TronowProvider(client).recover(attempt)
    client.submit_order.assert_not_awaited()


async def test_unknown_raw_code_saved_with_original_http_status(db_factory):
    await funded(db_factory)
    await reserve(db_factory)
    provider = FakeProvider()
    provider.submit = AsyncMock(
        side_effect=TronowApiError(
            "HTTP_ERROR",
            502,
            "req-future",
            raw_code="future-code-v2",
        )
    )
    await OrderWorker(db_factory, {provider.name: provider}, RentalSettings(batch_size=1)).tick()
    async with db_factory() as session:
        attempt = await session.scalar(select(PurchaseAttempt))
        assert attempt.error_code == "HTTP_ERROR" and attempt.last_raw_code == "future-code-v2"
        assert attempt.last_http_status == 502 and attempt.last_request_id == "req-future"
    await assert_money(db_factory, "6", "4")


@pytest.mark.parametrize("field", ["request_limit", "order_limit"])
def test_tronow_limit_validation(field):
    for invalid in (0, 1001):
        with pytest.raises(ValueError):
            TronowSettings.model_validate({field: invalid})
    settings = TronowSettings()
    assert (settings.request_limit, settings.order_limit) == (50, 10)


def test_tronow_limit_env_and_retry_budget(monkeypatch):
    monkeypatch.setenv("ENERGY_BOT_UPSTREAM__TRONOW__REQUEST_LIMIT", "30")
    monkeypatch.setenv("ENERGY_BOT_UPSTREAM__TRONOW__ORDER_LIMIT", "6")
    monkeypatch.setenv("ENERGY_BOT_RENTAL__MAX_SUBMIT_ATTEMPTS", "3")
    config = Settings()
    assert config.upstream.tronow.request_limit == 30
    assert config.upstream.tronow.order_limit == 6
    assert config.rental.max_submit_attempts == 3
    with pytest.raises(ValueError):
        RentalSettings(max_submit_attempts=0)


async def test_accepted_order_preserves_retry_after_and_request_id(db_factory):
    await funded(db_factory)
    order_id = await reserve(db_factory)
    client = AsyncMock(spec=TronowClient)
    client.get_quote.return_value = TronowQuote(65000, "1h", 2_000_000, "TRX", "now")
    client.get_balance.return_value = TronowBalance("TRX", 10_000_000, 0, 10_000_000, "now")

    async def accepted(body, *, idempotency_key):
        business_id = json.loads(body)["client_order_id"]
        return CreatedOrder(
            TronowOrderAccepted(
                "ord_accepted", business_id, TronowOrderStatus.PROCESSING, 2_000_000, "TRX", "now"
            ),
            "req-accepted",
            120,
        )

    client.submit_order.side_effect = accepted
    before = datetime.now(TIMEZONE)
    service = OrderWorker(
        db_factory, {"tronow": TronowProvider(client)}, RentalSettings(batch_size=1)
    )
    await service.tick()
    async with db_factory() as session:
        attempt = await session.scalar(select(PurchaseAttempt))
        assert attempt.last_request_id == "req-accepted" and attempt.last_http_status == 201
        order = await session.get(Order, order_id)
        assert order.next_run_at >= before + timedelta(seconds=120)
    await assert_money(db_factory, "6", "4")
