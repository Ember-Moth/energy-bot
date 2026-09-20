"""用户账本:仅可信的服务端调用入账,不接收 Telegram 自报充值。"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.models import Wallet, WalletEntry

UNIT = Decimal("0.000001")
MAX_AMOUNT = Decimal("99999999999999.999999")


class WalletError(ValueError):
    """余额不足、非法金额或同凭证异载荷。"""


def validate_amount(amount: Decimal) -> None:
    if (
        not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount <= 0
        or amount > MAX_AMOUNT
        or amount != amount.quantize(UNIT)
    ):
        raise WalletError("金额必须为正数,最多 6 位小数")


async def lock_wallet(session: AsyncSession, user_id: int) -> Wallet:
    await session.execute(
        insert(Wallet)
        .values(user_id=user_id, available=0, frozen=0)
        .on_conflict_do_nothing(index_elements=[Wallet.user_id])
    )
    return (
        await session.scalars(
            select(Wallet)
            .where(Wallet.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()


async def credit(session: AsyncSession, *, user_id: int, amount: Decimal, reference: str) -> Wallet:
    """外部已验证凭证入账;调用者负责验证与提交事务。reference 在全平台唯一。"""
    validate_amount(amount)
    if not reference or len(reference) > 128:
        raise WalletError("入账凭证长度须为 1–128")
    account = await lock_wallet(session, user_id)
    key = f"credit:{reference}"
    # 不同用户重放同一凭证由数据库主键裁决;冲突事务整体回滚。
    entry = await session.get(WalletEntry, key)
    if entry is not None:
        if entry.user_id != user_id or entry.available_delta != amount:
            raise WalletError("入账凭证已用于其他金额或用户")
        return account
    account.available += amount
    session.add(WalletEntry(key=key, user_id=user_id, available_delta=amount, frozen_delta=0))
    await session.flush()
    return account


async def hold(session: AsyncSession, *, user_id: int, order_id: int, amount: Decimal) -> None:
    validate_amount(amount)
    account = await lock_wallet(session, user_id)
    if account.available < amount:
        raise WalletError("余额不足")
    account.available -= amount
    account.frozen += amount
    session.add(
        WalletEntry(
            key=f"hold:{order_id}",
            user_id=user_id,
            order_id=order_id,
            available_delta=-amount,
            frozen_delta=amount,
        )
    )
    await session.flush()


async def settle(
    session: AsyncSession, *, user_id: int, order_id: int, amount: Decimal, capture: bool
) -> None:
    """调用前须持有订单行锁并校验 wallet_state;余额与订单在同一事务提交。"""
    account = await lock_wallet(session, user_id)
    if account.frozen < amount:
        raise WalletError("冻结余额不一致,需要人工对账")
    account.frozen -= amount
    if not capture:
        account.available += amount
    kind = "capture" if capture else "release"
    session.add(
        WalletEntry(
            key=f"{kind}:{order_id}",
            user_id=user_id,
            order_id=order_id,
            available_delta=Decimal(0) if capture else amount,
            frozen_delta=-amount,
        )
    )
    await session.flush()
