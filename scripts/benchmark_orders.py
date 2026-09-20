"""独立本地数据库 + 模拟 HTTP 上游的可复现订单并发基准。

必须指定 ENERGY_BOT_BENCHMARK_DSN,库名含 benchmark 且位于回环地址。
脚本清空该专用库的业务表,不会读取真实 config.yaml 或调用外部采购接口。
"""

import argparse
import asyncio
import json
import os
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import uvloop
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import delete, func, make_url, select

from energy_bot.config import (
    TIMEZONE,
    RentalSettings,
    TronbidSettings,
    TronowSettings,
    UpstreamSettings,
)
from energy_bot.db import create_engine_from_dsn, create_session_factory
from energy_bot.models import Base, Order, WalletEntry
from energy_bot.repositories.users import upsert_user
from energy_bot.services import rental, wallet
from energy_bot.services.procurement import OrderWorker
from energy_bot.services.providers import PurchaseResult, build_providers

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


class MeasuredWorker(OrderWorker):
    started: float = 0

    def __init__(self, *args):
        super().__init__(*args)
        self.latencies: list[float] = []

    async def _apply(
        self, order_id: int, token: str, attempt_id: int, result: PurchaseResult
    ) -> None:
        await super()._apply(order_id, token, attempt_id, result)
        self.latencies.append(time.perf_counter() - self.started)


async def benchmark(
    dsn: str, count: int, delay_ms: float, concurrency: int, mode: str
) -> list[dict]:
    engine = create_engine_from_dsn(dsn)
    factory = create_session_factory(engine)
    counters = {"quote": 0, "balance": 0, "orders": 0}
    charged: set[str] = set()

    async def upstream(request: web.Request) -> web.Response:
        route = request.path.rsplit("/", 1)[-1]
        counters[route] += 1
        await asyncio.sleep(delay_ms / 1000)
        if request.path.startswith("/openapi/v1/"):
            if route == "quote":
                data = {
                    "resource_amount": 65000,
                    "duration": "1h",
                    "price_sun": "3000000",
                    "currency": "TRX",
                    "priced_at": datetime.now(TIMEZONE).isoformat(),
                }
            elif route == "balance":
                data = {
                    "currency": "TRX",
                    "available_balance_sun": "1000000000000",
                    "reserved_balance_sun": "0",
                    "total_balance_sun": "1000000000000",
                    "updated_at": datetime.now(TIMEZONE).isoformat(),
                }
            else:
                raise AssertionError("基准应选择报价更低的 TronBid")
            return web.json_response({"code": "OK", "data": data})
        if route == "quote":
            return web.json_response(
                {"price_trx": "2.000000", "available": True, "expires_in_sec": 30}
            )
        if route == "balance":
            return web.json_response({"balance_trx": "1000000.000000"})
        data = await request.json()
        assert data["payment_mode"] == "balance"
        charged.add(data["idempotency_key"])
        return web.json_response(
            {
                "id": str(UUID(data["idempotency_key"][3:])),
                "status": "delegated",
                "payment_mode": "balance",
                "amount_trx": "2.000000",
                "energy_amount": data["energy_amount"],
                "duration_minutes": data["duration_minutes"],
                "target_address": data["target_address"],
            }
        )

    app = web.Application()
    app.router.add_route("*", "/api/v2/quick-rent/{route}", upstream)
    app.router.add_route("*", "/openapi/v1/{tail:.*}", upstream)
    results = []
    try:
        async with TestServer(app) as server:
            for workers, cache_seconds in ((1, 0), (concurrency, 0), (concurrency, 2)):
                async with factory() as session, session.begin():
                    for model in reversed(Base.metadata.sorted_tables):
                        await session.execute(delete(model))
                charged.clear()
                counters.update(dict.fromkeys(counters, 0))
                async with factory() as session, session.begin():
                    for i in range(count):
                        await upsert_user(
                            session, user_id=i + 1, first_name="基准用户", language_code="zh"
                        )
                        await wallet.credit(
                            session, user_id=i + 1, amount=Decimal("10"), reference=f"bench-{i}"
                        )
                        await rental.reserve_order(
                            session,
                            user_id=i + 1,
                            request_key=f"bench-{i}",
                            recipient_address=ADDRESS,
                            energy_amount=65000,
                            duration_minutes=60,
                            price=Decimal("4"),
                        )
                settings = RentalSettings(
                    order_concurrency=workers,
                    batch_size=count,
                    quote_cache_seconds=cache_seconds,
                    balance_cache_seconds=min(cache_seconds, 1),
                    upstream_requests_per_second=1000,
                    upstream_orders_per_second=1000,
                )
                providers = build_providers(
                    UpstreamSettings(
                        tronow=TronowSettings(
                            base_url=str(server.make_url("/openapi/v1")),
                            api_key="synthetic-second" if mode == "multi" else "",
                            api_secret=secrets.token_hex(16),
                            request_limit=1000,
                            order_limit=1000,
                        ),
                        tronbid=TronbidSettings(
                            base_url=str(server.make_url("/api/v2/quick-rent")),
                            api_key="synthetic-benchmark",
                        ),
                    ),
                    factory,
                    settings,
                )
                worker = MeasuredWorker(factory, providers, settings)
                worker.started = time.perf_counter()
                try:
                    await worker.tick()
                    elapsed = time.perf_counter() - worker.started
                    async with factory() as session:
                        captured = await session.scalar(
                            select(func.count())
                            .select_from(Order)
                            .where(Order.wallet_state == "captured")
                        )
                        entries = await session.scalar(
                            select(func.count())
                            .select_from(WalletEntry)
                            .where(WalletEntry.key.startswith("capture:"))
                        )
                    assert captured == entries == len(charged) == count, "订单或资金验证失败"
                    assert len(worker.latencies) == count
                    if mode == "single":
                        assert counters["quote"] == counters["balance"] == 0
                    else:
                        assert counters["quote"] > 0 and counters["balance"] > 0
                    ordered = sorted(worker.latencies)
                    results.append(
                        {
                            "mode": mode,
                            "orders": count,
                            "concurrency": workers,
                            "cache_seconds": cache_seconds,
                            "delay_ms_per_http": delay_ms,
                            "seconds": round(elapsed, 3),
                            "orders_per_second": round(count / elapsed, 2),
                            "p50_queue_to_settle_seconds": round(statistics.median(ordered), 3),
                            "p95_queue_to_settle_seconds": round(
                                ordered[max(0, int(count * 0.95) - 1)], 3
                            ),
                            "http_calls": dict(counters),
                            "captures": captured,
                        }
                    )
                    print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                finally:
                    await worker.close()
    finally:
        await engine.dispose()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("single", "multi"), default="single")
    parser.add_argument("--orders", type=int, default=80)
    parser.add_argument("--delay-ms", type=float, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 2 <= args.orders <= 100 or not 1 <= args.concurrency <= 64 or args.delay_ms < 0:
        parser.error("orders=2–100, concurrency=1–64, delay-ms>=0")
    dsn = os.environ.get("ENERGY_BOT_BENCHMARK_DSN", "")
    if not dsn:
        parser.error("必须设置专用 ENERGY_BOT_BENCHMARK_DSN")
    url = make_url(dsn)
    if url.host not in ("localhost", "127.0.0.1", "::1") or "benchmark" not in (url.database or ""):
        parser.error("仅允许本机回环地址且库名含 benchmark 的独立测试库")
    with tempfile.TemporaryDirectory(prefix="energy-benchmark-") as directory:
        config = Path(directory) / "config.yaml"
        config.write_text("{}", encoding="utf-8")
        env = {key: val for key, val in os.environ.items() if not key.startswith("ENERGY_BOT_")}
        env.update(ENERGY_BOT_CONFIG=str(config), ENERGY_BOT_DATABASE__DSN=dsn)
        subprocess.run(  # noqa: S603 -- 固定 Python/Alembic 命令,不经过 shell
            [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
            env=env,
            check=True,
        )
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        results = runner.run(
            benchmark(dsn, args.orders, args.delay_ms, args.concurrency, args.mode)
        )
    if args.output:
        args.output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
