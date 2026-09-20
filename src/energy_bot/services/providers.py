"""上游协议适配:同规格报价、余额支付及不确定采购的原单恢复。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from energy_bot.config import TIMEZONE, UpstreamSettings
from energy_bot.models import PurchaseAttempt
from energy_bot.services.upstream.tronbid import TronbidClient, TronbidOrder
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient, TronowOrder

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
    expires_at: datetime | None = None
    txid: str = ""


class Provider(Protocol):
    name: str

    async def quote(self, product: Product) -> Offer | None: ...
    def body(self, product: Product, business_id: str) -> str: ...
    async def submit(self, attempt: PurchaseAttempt) -> PurchaseResult: ...
    async def recover(self, attempt: PurchaseAttempt) -> PurchaseResult: ...
    async def close(self) -> None: ...


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ProviderMismatch("上游时间缺少时区")
    return result


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

    async def quote(self, product: Product) -> Offer | None:
        if product.minutes != 60:
            return None
        observed, balance = await asyncio.gather(
            _observed(self.client.get_quote(product.energy)),
            self.client.get_balance(),
        )
        quote, received_at = observed
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
            received_at + timedelta(seconds=10),
        )

    def body(self, product: Product, business_id: str) -> str:
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
        result = (
            await self.client.submit_order(
                attempt.request_body.encode(),
                idempotency_key=attempt.idempotency_key,
            )
        ).accepted
        if result.client_order_id != attempt.business_id or result.currency != "TRX":
            raise ProviderMismatch("TRONow 受理结果身份不符")
        # 受理结果不含交付详情;包括 SUCCESS 重放也先保存单号,下一轮查询完整结果。
        return PurchaseResult(result.order_id, "pending", Decimal(result.reserved_amount_sun) / SUN)

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
        expiry = _time(result.lease_expires_at)
        if expiry is None and result.confirmed_at:
            confirmed = _time(result.confirmed_at)
            assert confirmed is not None
            expiry = confirmed + timedelta(hours=1)
        state = states.get(result.status, "pending")
        if state == "success" and expiry is None:
            state = "reviewing"
        return PurchaseResult(
            result.order_id, state, Decimal(result.amount_sun) / SUN, expiry, result.txid or ""
        )

    async def recover(self, attempt: PurchaseAttempt) -> PurchaseResult:
        if attempt.upstream_order_id:
            return self._result(attempt, await self.client.get_order(attempt.upstream_order_id))
        try:
            result = await self.client.get_order_by_client_id(attempt.business_id)
        except TronowApiError as exc:
            if exc.code != "ORDER_NOT_FOUND":
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
        if (
            not quote.available
            or quote.expires_in_sec <= 0
            or balance.balance_trx < quote.price_trx
        ):
            return None
        return Offer(
            self.name,
            quote.price_trx,
            received_at + timedelta(seconds=min(quote.expires_in_sec, 30)),
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


def build_providers(settings: UpstreamSettings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if settings.tronow.api_key and settings.tronow.api_secret:
        providers["tronow"] = TronowProvider(TronowClient(settings.tronow))
    if settings.tronbid.api_key:
        providers["tronbid"] = TronbidProvider(TronbidClient(settings.tronbid))
    return providers
