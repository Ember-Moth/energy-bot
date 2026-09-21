"""上游协议适配:同规格报价、余额支付及不确定采购的原单恢复。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import TIMEZONE, RentalSettings, UpstreamSettings
from energy_bot.models import PurchaseAttempt
from energy_bot.services.cached_upstream import CachedTronbidClient, CachedTronowClient
from energy_bot.services.upstream.tronbid import TronbidClient, TronbidOrder
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient, TronowOrder
from energy_bot.services.upstream_cache import PostgresCache, cache_key
from energy_bot.services.upstream_gate import TronowGate, UpstreamGate

SUN = Decimal(1000000)


class ProviderMismatch(ValueError):
    """返回身份或规格与持久化请求不一致,不得结算或重新购买。"""


class ManualReview(ValueError):
    """原请求需要人工核实,不能自动重放。"""


@dataclass(frozen=True)
class Product:
    address: str
    energy: int
    minutes: int


@dataclass(frozen=True)
class Offer:
    provider: str
    cost: Decimal
    valid_until: datetime


@dataclass(frozen=True)
class PurchaseResult:
    upstream_id: str
    state: str
    cost: Decimal
    txid: str = ""
    retry_after: int | None = None
    request_id: str | None = None
    http_status: int | None = None


class Provider(Protocol):
    name: str

    def supports(self, product: Product) -> bool: ...
    async def quote(self, product: Product) -> Offer | None: ...
    def body(self, product: Product, business_id: str) -> str: ...
    async def submit(self, attempt: PurchaseAttempt) -> PurchaseResult: ...
    async def recover(self, attempt: PurchaseAttempt) -> PurchaseResult: ...
    async def close(self) -> None: ...


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def _observed[T](request: Awaitable[T]) -> tuple[T, datetime]:
    result = await request
    return result, datetime.now(TIMEZONE)


class TronowProvider:
    name = "tronow"

    def __init__(self, client: TronowClient) -> None:
        self.client = client

    async def close(self) -> None:
        await self.client.close()

    def supports(self, product: Product) -> bool:
        return product.energy > 0 and product.minutes == 60

    async def quote(self, product: Product) -> Offer | None:
        if not self.supports(product):
            return None
        observed, balance = await asyncio.gather(
            _observed(self.client.get_quote(product.energy)),
            self.client.get_balance(),
        )
        quote, received_at = observed
        cached_until = getattr(quote, "valid_until", None)
        if cached_until is not None and cached_until <= datetime.now(TIMEZONE):
            quote, received_at = await _observed(self.client.get_quote(product.energy))
        if (
            quote.resource_amount != product.energy
            or quote.duration != "1h"
            or quote.currency != "TRX"
            or balance.currency != "TRX"
        ):
            raise ProviderMismatch("TRONow 报价规格或币种不符")
        if balance.available_balance_sun < quote.price_sun:
            return None
        # 报价不锁价,本地仅允许短时使用;最终成本以受理/查询为准。
        return Offer(
            self.name,
            Decimal(quote.price_sun) / SUN,
            getattr(quote, "valid_until", None) or received_at + timedelta(seconds=10),
        )

    def body(self, product: Product, business_id: str) -> str:
        if not self.supports(product):
            raise ProviderMismatch("TRONow 仅支持正整数能量和 60 分钟租期")
        return _json(
            {
                "client_order_id": business_id,
                "resource_type": "ENERGY",
                "receiver_address": product.address,
                "resource_amount": product.energy,
                "duration": "1h",
            }
        )

    async def submit(self, attempt: PurchaseAttempt) -> PurchaseResult:
        created = await self.client.submit_order(
            attempt.request_body.encode(),
            idempotency_key=attempt.idempotency_key,
        )
        result = created.accepted
        if result.client_order_id != attempt.business_id or result.currency != "TRX":
            raise ProviderMismatch("TRONow 受理结果身份不符")
        # 受理结果不含交付详情;包括 SUCCESS 重放也先保存单号,下一轮查询完整结果。
        return PurchaseResult(
            result.order_id,
            "pending",
            Decimal(result.reserved_amount_sun) / SUN,
            retry_after=created.retry_after,
            request_id=created.request_id,
            http_status=created.http_status,
        )

    def _result(self, attempt: PurchaseAttempt, result: TronowOrder) -> PurchaseResult:
        body = json.loads(attempt.request_body)
        if (
            result.client_order_id != attempt.business_id
            or result.currency != "TRX"
            or result.receiver_address != body["receiver_address"]
            or result.resource_amount != body["resource_amount"]
            or result.duration != "1h"
            or (attempt.upstream_order_id and result.order_id != attempt.upstream_order_id)
        ):
            raise ProviderMismatch("TRONow 查询身份或规格不符")
        states = {"SUCCESS": "success", "FAILED": "failed", "REVIEWING": "reviewing"}
        return PurchaseResult(
            result.order_id,
            states.get(result.status, "pending"),
            Decimal(result.amount_sun) / SUN,
            result.txid or "",
            retry_after=result.retry_after,
            request_id=result.request_id,
            http_status=result.http_status,
        )

    async def recover(self, attempt: PurchaseAttempt) -> PurchaseResult:
        if attempt.upstream_order_id:
            return self._result(attempt, await self.client.get_order(attempt.upstream_order_id))
        try:
            result = await self.client.get_order_by_client_id(attempt.business_id)
        except TronowApiError as exc:
            if exc.status != 404 or exc.code != "ORDER_NOT_FOUND":
                raise
            if attempt.state == "reviewing":
                raise ManualReview("原请求未匹配,等待人工核对") from exc
            # 先查业务号,确认未受理后只重放原始请求,不切换供应商。
            return await self.submit(attempt)
        return self._result(attempt, result)


class TronbidProvider:
    name = "tronbid"

    def __init__(self, client: TronbidClient) -> None:
        self.client = client

    async def close(self) -> None:
        await self.client.close()

    def supports(self, product: Product) -> bool:
        return product.energy > 0 and product.minutes > 0

    async def quote(self, product: Product) -> Offer | None:
        observed, balance = await asyncio.gather(
            _observed(
                self.client.create_quote(
                    energy_amount=product.energy, duration_minutes=product.minutes
                )
            ),
            self.client.get_balance(),
        )
        quote, received_at = observed
        cached_until = getattr(quote, "valid_until", None)
        if cached_until is not None and cached_until <= datetime.now(TIMEZONE):
            quote, received_at = await _observed(
                self.client.create_quote(
                    energy_amount=product.energy,
                    duration_minutes=product.minutes,
                )
            )
        if (
            not quote.available
            or quote.expires_in_sec <= 0
            or balance.balance_trx < quote.price_trx
        ):
            return None
        return Offer(
            self.name,
            quote.price_trx,
            getattr(quote, "valid_until", None)
            or received_at + timedelta(seconds=min(quote.expires_in_sec, 30)),
        )

    def body(self, product: Product, business_id: str) -> str:
        return _json(
            {
                "idempotency_key": business_id,
                "target_address": product.address,
                "energy_amount": product.energy,
                "duration_minutes": product.minutes,
                "payment_mode": "balance",
            }
        )

    def _result(self, attempt: PurchaseAttempt, result: TronbidOrder) -> PurchaseResult:
        body = json.loads(attempt.request_body)
        if (
            result.payment_mode != "balance"
            or result.target_address != body["target_address"]
            or result.energy_amount != body["energy_amount"]
            or result.duration_minutes != body["duration_minutes"]
            or (attempt.upstream_order_id and result.id != attempt.upstream_order_id)
        ):
            raise ProviderMismatch("TronBid 查询身份或规格不符")
        state = {
            "delegated": "success",
            "failed": "failed",
            "cancelled": "failed",
            "expired": "expired",
            "pending_payment": "reviewing",
        }.get(result.status, "pending")
        if (
            state == "success"
            and result.effective_energy_amount is not None
            and result.effective_energy_amount < body["energy_amount"]
        ):
            state = "reviewing"
        # expires_at 未明确是付款截止还是租赁到期,不能据此推算用户租期。
        return PurchaseResult(result.id, state, result.amount_trx)

    async def submit(self, attempt: PurchaseAttempt) -> PurchaseResult:
        return self._result(attempt, await self.client.submit_order(attempt.request_body.encode()))

    async def recover(self, attempt: PurchaseAttempt) -> PurchaseResult:
        if attempt.upstream_order_id:
            return self._result(attempt, await self.client.get_order(attempt.upstream_order_id))
        if attempt.state == "reviewing":
            raise ManualReview("原请求需要人工核对")
        # 无按业务号查单接口,只能按协议使用同一幂等键和完全相同载荷重放。
        return await self.submit(attempt)


def build_providers(
    settings: UpstreamSettings,
    factory: async_sessionmaker[AsyncSession] | None = None,
    rental: RentalSettings | None = None,
) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    rental = rental or RentalSettings()
    cache = PostgresCache(factory) if factory is not None else None
    if settings.tronow.api_key and settings.tronow.api_secret:
        config = settings.tronow
        client = TronowClient(config)
        if factory is not None and cache is not None:
            scope = cache_key("tronow", config.account_scope or "default-merchant")
            client = CachedTronowClient(
                config,
                cache,
                rental,
                TronowGate(
                    factory,
                    scope,
                    rental,
                    request_limit=config.request_limit,
                    order_limit=config.order_limit,
                ),
            )
        providers["tronow"] = TronowProvider(client)
    if settings.tronbid.api_key:
        config_bid = settings.tronbid
        client_bid = TronbidClient(config_bid)
        if factory is not None and cache is not None:
            scope = cache_key(
                "tronbid", config_bid.base_url, config_bid.account_scope or config_bid.api_key
            )
            client_bid = CachedTronbidClient(
                config_bid, cache, rental, UpstreamGate(factory, scope, rental)
            )
        providers["tronbid"] = TronbidProvider(client_bid)
    return providers
