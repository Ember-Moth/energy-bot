"""审计发现的协议、配置与启动回归测试。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import make_url

from energy_bot import app as bot_app
from energy_bot.config import DatabaseSettings, Settings, TronbidSettings, TronowSettings
from energy_bot.services.upstream.tronbid import TronbidApiError, TronbidClient
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.mark.parametrize("password", ["synthetic pass", "synthetic+%/@?#", "测试密码"])
def test_database_credentials_roundtrip(password: str) -> None:
    settings = DatabaseSettings(
        address="::1", username="audit @user", password=password, database="test"
    )
    url = make_url(settings.effective_dsn())
    assert url.username == "audit @user"
    assert url.password == password
    assert url.host == "::1"


@pytest.mark.parametrize("status", [200, 502])
@pytest.mark.parametrize("provider", ["tronow", "tronbid"])
async def test_non_utf8_response_keeps_http_metadata(provider: str, status: int) -> None:
    async def respond(request: web.Request) -> web.Response:
        return web.Response(status=status, body=b"\xff", headers={"Retry-After": "3"})

    app = web.Application()
    app.router.add_get("/{tail:.*}", respond)
    async with TestClient(TestServer(app)) as server:
        if provider == "tronow":
            client = TronowClient(TronowSettings(base_url=str(server.make_url("/openapi/v1"))))
            error = TronowApiError
        else:
            client = TronbidClient(TronbidSettings(base_url=str(server.make_url("/api/v2"))))
            error = TronbidApiError
        async with client:
            with pytest.raises(error) as exc:
                await client.get_balance()
        assert exc.value.code == ("HTTP_ERROR" if status == 502 else "INVALID_RESPONSE")
        assert exc.value.status == status
        assert exc.value.retry_after == 3


@pytest.mark.parametrize("business_id", ["a", "eb-1", "eb-000001", "x" * 64])
async def test_tronow_business_id_and_stable_idempotency(business_id: str) -> None:
    received = []

    async def create(request: web.Request) -> web.Response:
        body = await request.json()
        received.append((body, request.headers["Idempotency-Key"], request.headers["X-Nonce"]))
        return web.json_response(
            {
                "code": "OK",
                "data": {
                    "order_id": "ord_test",
                    "client_order_id": body["client_order_id"],
                    "status": "PROCESSING",
                    "reserved_amount_sun": "1",
                    "currency": "TRX",
                    "created_at": "2026-09-20T00:00:00+08:00",
                },
            }
        )

    app = web.Application()
    app.router.add_post("/openapi/v1/orders", create)
    async with TestClient(TestServer(app)) as server:
        async with TronowClient(TronowSettings(base_url=str(server.make_url("/openapi/v1")))) as c:
            for _ in range(2):
                await c.create_order(
                    client_order_id=business_id, receiver_address="T" * 34, resource_amount=65000
                )
            await c.create_order(
                client_order_id=business_id,
                receiver_address="T" * 34,
                resource_amount=65000,
                idempotency_key="explicit-stable-key",
            )
    assert received[0][0]["client_order_id"] == business_id
    assert received[0][:2] == received[1][:2]
    assert 8 <= len(received[0][1]) <= 128
    assert received[0][2] != received[1][2]
    assert received[2][1] == "explicit-stable-key"
    if len(business_id) >= 8:
        assert received[0][1] == business_id  # 已有订单的默认幂等键保持不变
    else:
        assert len(received[0][1]) > 64  # 不与直接使用业务单号的键空间重叠


@pytest.mark.parametrize("enabled", [False, True])
async def test_startup_preserves_pending_updates(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    settings = Settings(
        bot_token="synthetic", database=DatabaseSettings(dsn="postgresql://u@localhost/test")
    )
    settings.webhook.base_url = "https://example.com"
    settings.rental.enabled = enabled
    worker = MagicMock(close=AsyncMock())
    monkeypatch.setattr(bot_app, "OrderWorker", lambda *args: worker)
    monkeypatch.setattr(bot_app, "load_settings", lambda _: settings)
    monkeypatch.setattr(bot_app, "setup_logging", MagicMock())
    engine = MagicMock(dispose=AsyncMock())
    monkeypatch.setattr(bot_app, "create_engine_from_dsn", lambda *a, **kw: engine)
    monkeypatch.setattr(bot_app, "create_session_factory", MagicMock())
    bot = MagicMock(set_webhook=AsyncMock(), delete_webhook=AsyncMock())
    bot.session.close = AsyncMock()
    monkeypatch.setattr(bot_app, "Bot", lambda **kw: bot)
    monkeypatch.setattr(bot_app, "Dispatcher", MagicMock())
    for name in (
        "register_telegram_routes",
        "register_health_routes",
        "register_tronow_webhook",
        "setup_application",
    ):
        monkeypatch.setattr(bot_app, name, MagicMock())
    runner = MagicMock(setup=AsyncMock(), cleanup=AsyncMock())
    monkeypatch.setattr(bot_app.web, "AppRunner", lambda *a, **kw: runner)
    monkeypatch.setattr(bot_app.web, "TCPSite", lambda *a, **kw: MagicMock(start=AsyncMock()))
    stop = asyncio.Event()
    stop.set()
    monkeypatch.setattr(bot_app.asyncio, "Event", lambda: stop)
    await bot_app.amain()
    assert bot.set_webhook.await_args.kwargs["drop_pending_updates"] is False
    bot.delete_webhook.assert_awaited_once()
    engine.dispose.assert_awaited_once()
    assert worker.start.call_count == int(enabled)
    assert worker.close.await_count == int(enabled)


async def test_alembic_cli_from_other_directory(db_factory, tmp_path: Path) -> None:
    """在专用测试库实际升级/降级,覆盖密码传递和不依赖工作目录。"""
    ini = ALEMBIC_INI
    for arguments in [
        ("downgrade", "base"),
        ("upgrade", "head"),
        ("downgrade", "-1"),
        ("upgrade", "head"),
        ("check",),
    ]:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "-c", str(ini), *arguments],
            cwd=tmp_path,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
