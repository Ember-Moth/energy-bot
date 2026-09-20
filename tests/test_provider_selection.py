"""报价资格与实际交付规格不能被最低价选择忽略。"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from energy_bot.models import PurchaseAttempt
from energy_bot.services.providers import Product, TronbidProvider, TronowProvider
from energy_bot.services.upstream.tronbid import TronbidClient
from energy_bot.services.upstream.tronow import TronowClient

ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


@pytest.mark.parametrize(
    "available,cost,balance,ttl",
    [
        (False, "1", "10", 30),
        (True, "11", "10", 30),
        (True, "1", "10", 0),
    ],
)
async def test_tronbid_filters_unavailable_and_underfunded(available, cost, balance, ttl):
    client = AsyncMock(spec=TronbidClient)
    client.create_quote.return_value = SimpleNamespace(
        available=available, price_trx=Decimal(cost), expires_in_sec=ttl
    )
    client.get_balance.return_value = SimpleNamespace(balance_trx=Decimal(balance))
    assert await TronbidProvider(client).quote(Product(ADDRESS, 65000, 15)) is None


async def test_tronow_never_quotes_wrong_duration():
    client = AsyncMock(spec=TronowClient)
    assert await TronowProvider(client).quote(Product(ADDRESS, 65000, 15)) is None
    client.get_quote.assert_not_called()
    client.get_balance.assert_not_called()


async def test_tronbid_partial_delivery_requires_review():
    client = AsyncMock(spec=TronbidClient)
    provider = TronbidProvider(client)
    attempt = PurchaseAttempt(
        business_id="business-1",
        idempotency_key="business-1",
        request_body=provider.body(Product(ADDRESS, 65000, 15), "business-1"),
    )
    client.submit_order.return_value = SimpleNamespace(
        id="remote-id",
        payment_mode="balance",
        target_address=ADDRESS,
        energy_amount=65000,
        duration_minutes=15,
        status="delegated",
        effective_energy_amount=32000,
        amount_trx=Decimal("1"),
    )
    assert (await provider.submit(attempt)).state == "reviewing"
