"""只缓存报价与余额;查单、采购及用户账本从不读取缓存。"""

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal

from energy_bot.config import TIMEZONE, RentalSettings, TronbidSettings, TronowSettings
from energy_bot.services.upstream.tronbid import TronbidBalance, TronbidClient, TronbidQuote
from energy_bot.services.upstream.tronow import (
    CreatedOrder,
    TronowBalance,
    TronowClient,
    TronowQuote,
)
from energy_bot.services.upstream_cache import CachedValue, PostgresCache, cache_key
from energy_bot.services.upstream_gate import UpstreamGate


class CachedTronowClient(TronowClient):
    def __init__(
        self,
        settings: TronowSettings,
        cache: PostgresCache,
        rental: RentalSettings,
        gate: UpstreamGate,
    ):
        super().__init__(settings, gate=gate)
        self.cache = cache
        self.rental = rental
        self.scope = cache_key(
            "tronow-read-v1", settings.base_url, settings.api_key, settings.api_secret
        )
        self.balance_key = cache_key(self.scope, "balance")

    async def get_quote(self, resource_amount: int) -> TronowQuote:
        if self.rental.quote_cache_seconds == 0:
            return await super().get_quote(resource_amount)
        fetch = super().get_quote

        async def load() -> CachedValue:
            quote = await fetch(resource_amount)
            return CachedValue(
                asdict(quote),
                datetime.now(TIMEZONE) + timedelta(seconds=self.rental.quote_cache_seconds),
            )

        result = await self.cache.get(cache_key(self.scope, "quote", resource_amount, 60), load)
        quote = TronowQuote(**result.payload)
        return replace(quote, valid_until=result.expires_at)

    async def get_balance(self) -> TronowBalance:
        if self.rental.balance_cache_seconds == 0:
            return await super().get_balance()
        fetch = super().get_balance

        async def load() -> CachedValue:
            return CachedValue(
                asdict(await fetch()),
                datetime.now(TIMEZONE) + timedelta(seconds=self.rental.balance_cache_seconds),
            )

        result = await self.cache.get(self.balance_key, load)
        return TronowBalance(**result.payload)

    async def submit_order(self, body: bytes, *, idempotency_key: str) -> CreatedOrder:
        await self.cache.invalidate(self.balance_key)
        try:
            return await super().submit_order(body, idempotency_key=idempotency_key)
        finally:
            await self.cache.invalidate(self.balance_key)

    async def close(self) -> None:
        await self.cache.close()
        await super().close()


class CachedTronbidClient(TronbidClient):
    def __init__(
        self,
        settings: TronbidSettings,
        cache: PostgresCache,
        rental: RentalSettings,
        gate: UpstreamGate,
    ):
        super().__init__(settings, gate=gate)
        self.cache = cache
        self.rental = rental
        self.scope = cache_key("tronbid-read-v1", settings.base_url, settings.api_key)
        self.balance_key = cache_key(self.scope, "balance")

    async def create_quote(self, *, energy_amount: int, duration_minutes: int) -> TronbidQuote:
        if self.rental.quote_cache_seconds == 0:
            return await super().create_quote(
                energy_amount=energy_amount, duration_minutes=duration_minutes
            )
        fetch = super().create_quote

        async def load() -> CachedValue:
            quote = await fetch(energy_amount=energy_amount, duration_minutes=duration_minutes)
            payload = asdict(quote)
            payload["price_trx"] = str(quote.price_trx)
            seconds = min(self.rental.quote_cache_seconds, quote.expires_in_sec)
            return CachedValue(payload, datetime.now(TIMEZONE) + timedelta(seconds=seconds))

        result = await self.cache.get(
            cache_key(self.scope, "quote", energy_amount, duration_minutes), load
        )
        payload = {**result.payload, "price_trx": Decimal(result.payload["price_trx"])}
        return replace(TronbidQuote(**payload), valid_until=result.expires_at)

    async def get_balance(self) -> TronbidBalance:
        if self.rental.balance_cache_seconds == 0:
            return await super().get_balance()
        fetch = super().get_balance

        async def load() -> CachedValue:
            balance = await fetch()
            return CachedValue(
                {"balance_trx": str(balance.balance_trx)},
                datetime.now(TIMEZONE) + timedelta(seconds=self.rental.balance_cache_seconds),
            )

        result = await self.cache.get(self.balance_key, load)
        return TronbidBalance(balance_trx=Decimal(result.payload["balance_trx"]))

    async def submit_order(self, body: bytes):
        await self.cache.invalidate(self.balance_key)
        try:
            return await super().submit_order(body)
        finally:
            await self.cache.invalidate(self.balance_key)

    async def close(self) -> None:
        await self.cache.close()
        await super().close()
