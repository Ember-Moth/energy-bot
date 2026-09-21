"""能量租赁:订单状态机与工作流。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from energy_bot.config import TIMEZONE
from energy_bot.models import Order, OrderNotification, OrderStatus, PurchaseAttempt
from energy_bot.repositories import orders as order_repo
from energy_bot.services import wallet

# T 开头 + 33 个 base58 字符(排除 0OIl)
TRON_ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TRON_ADDRESS_PREFIX = 0x41  # 主网地址解码后的首字节

# 允许的状态跃迁;ACTIVE / REFUNDED 为终态不再迁出。
# 全部订单走余额冻结流程:draft 仅在建单瞬间存在,delegating 的结算只经
# complete_purchase(到账扣款)/ release_order(失败解冻)。
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset({OrderStatus.RESERVED}),
    OrderStatus.RESERVED: frozenset({OrderStatus.DELEGATING, OrderStatus.REFUNDED}),
    OrderStatus.DELEGATING: frozenset({OrderStatus.ACTIVE, OrderStatus.REFUNDED}),
    OrderStatus.ACTIVE: frozenset(),
    OrderStatus.REFUNDED: frozenset(),
}


class RentalError(ValueError):
    """业务规则违反。"""


class InvalidTransitionError(RentalError):
    """非法状态跃迁。"""


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _b58decode(s: str) -> bytes:
    """base58 解码(仅限字母表内字符,调用前先用 TRON_ADDRESS_RE 过滤)。"""
    num = 0
    for ch in s:
        num = num * 58 + BASE58_ALPHABET.index(ch)
    payload = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))  # 前导 '1' 代表前导零字节
    return b"\x00" * pad + payload


def is_valid_tron_address(address: str) -> bool:
    """base58check 全量校验:格式 + 主网前缀 + 双 SHA-256 校验和。

    仅过正则是远远不够的:打错一个字符的地址同样是 34 位 base58,
    能量委托过去将无法收回。
    """
    if not TRON_ADDRESS_RE.fullmatch(address):
        return False
    raw = _b58decode(address)
    if len(raw) != 25 or raw[0] != TRON_ADDRESS_PREFIX:
        return False
    body, checksum = raw[:-4], raw[-4:]
    digest = hashlib.sha256(hashlib.sha256(body).digest()).digest()
    return digest[:4] == checksum


def ensure_transition(order: Order, to: OrderStatus) -> None:
    """校验状态跃迁是否合法,非法即抛 InvalidTransitionError。"""
    if to not in ALLOWED_TRANSITIONS[order.status]:
        raise InvalidTransitionError(
            f"订单 {order.id} 不允许从 {order.status.value} 转为 {to.value}"
        )


async def _get_locked(session: AsyncSession, order_id: int) -> Order:
    order = await order_repo.get_order_for_update(session, order_id)
    if order is None:
        raise RentalError(f"订单不存在:{order_id}")
    return order


async def reserve_order(
    session: AsyncSession,
    *,
    user_id: int,
    request_key: str,
    recipient_address: str,
    energy_amount: int,
    duration_minutes: int,
    price: Decimal,
    max_cost: Decimal | None = None,
) -> Order:
    """幂等创建余额订单并冻结销售金额;销售价只能由可信服务端传入。"""
    wallet.validate_amount(price)
    max_cost = price if max_cost is None else max_cost
    wallet.validate_amount(max_cost)
    if price > Decimal("999999.999999"):
        raise RentalError("订单金额超过上限")
    if not request_key or len(request_key) > 128:
        raise RentalError("request_key 长度须为 1–128")
    if not is_valid_tron_address(recipient_address):
        raise RentalError("无效的 TRON 地址")
    if type(energy_amount) is not int or not 1 <= energy_amount <= 2147483647:
        raise RentalError("能量数量必须为有效正整数")
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 525600:
        raise RentalError("租期必须为 1–525600 分钟")
    # 串行化同用户新订单,避免同余额并发超扣;已存在订单只读返回,不反向等待订单锁。
    await wallet.lock_wallet(session, user_id)
    existing = await session.scalar(
        select(Order).where(
            Order.user_id == user_id,
            Order.request_key == request_key,
        )
    )
    if existing is not None:
        if (
            existing.recipient_address,
            existing.energy_amount,
            existing.duration_minutes,
            existing.price,
            existing.max_cost,
        ) != (recipient_address, energy_amount, duration_minutes, price, max_cost):
            raise RentalError("相同请求标识不能用于不同订单")
        return existing
    order = Order(
        user_id=user_id,
        request_key=request_key,
        recipient_address=recipient_address,
        energy_amount=energy_amount,
        duration_minutes=duration_minutes,
        price=price,
        max_cost=max_cost,
        status=OrderStatus.DRAFT,
        wallet_state="held",
        next_run_at=_now(),
    )
    session.add(order)
    await session.flush()
    await wallet.hold(session, user_id=user_id, order_id=order.id, amount=price)
    ensure_transition(order, OrderStatus.RESERVED)
    order.status = OrderStatus.RESERVED
    await session.flush()
    return order


async def notify_order(session: AsyncSession, order: Order, event: str, text: str) -> None:
    await session.execute(
        insert(OrderNotification)
        .values(
            order_id=order.id,
            user_id=order.user_id,
            event=event,
            text=text,
            next_run_at=_now(),
        )
        .on_conflict_do_nothing(
            index_elements=[OrderNotification.order_id, OrderNotification.event]
        )
    )


async def begin_purchase(session: AsyncSession, order: Order, provider: str) -> None:
    """调用方持有订单行锁,并在发请求前提交采购意图。"""
    if order.wallet_state != "held":
        raise RentalError("订单没有可用冻结款")
    if order.status is not OrderStatus.DELEGATING:
        ensure_transition(order, OrderStatus.DELEGATING)
        order.status = OrderStatus.DELEGATING
    order.provider = provider
    order.upstream_order_id = None
    order.purchase_cost = None
    order.upstream_txid = None
    order.delegated_at = _now()
    await session.flush()


async def complete_purchase(
    session: AsyncSession,
    order: Order,
    *,
    cost: Decimal,
    txid: str = "",
) -> None:
    """受订单行锁保护的采购成功与扣款。"""
    if order.wallet_state == "captured":
        return
    if order.wallet_state != "held":
        raise RentalError("已释放的订单不能再次扣款")
    ensure_transition(order, OrderStatus.ACTIVE)
    await wallet.settle(
        session, user_id=order.user_id, order_id=order.id, amount=order.price, capture=True
    )
    order.wallet_state = "captured"
    order.purchase_cost = cost
    order.status = OrderStatus.ACTIVE
    order.upstream_txid = txid or None
    await notify_order(
        session, order, "fulfilled", f"订单 #{order.id} 能量已到账，已扣款 {order.price:f} TRX。"
    )
    await session.flush()


async def release_order(session: AsyncSession, order: Order, *, reason: str) -> None:
    """仅在尚未提交或全部采购已有明确失败结果时释放冻结款。"""
    if order.wallet_state == "released":
        return
    if order.wallet_state != "held":
        raise RentalError("订单已结算,不能释放冻结款")
    uncertain = await session.scalar(
        select(PurchaseAttempt.id)
        .where(
            PurchaseAttempt.order_id == order.id,
            PurchaseAttempt.state.not_in(("failed", "rejected")),
        )
        .limit(1)
    )
    if uncertain is not None:
        raise RentalError("上游结果尚未确认,不能解冻或退款")
    ensure_transition(order, OrderStatus.REFUNDED)
    await wallet.settle(
        session, user_id=order.user_id, order_id=order.id, amount=order.price, capture=False
    )
    order.wallet_state = "released"
    order.status = OrderStatus.REFUNDED
    order.last_error = reason
    order.next_run_at = None
    await notify_order(
        session, order, "refunded", f"订单 #{order.id} 未完成，{order.price:f} TRX 已退回可用余额。"
    )
    await session.flush()


async def cancel_order(session: AsyncSession, *, user_id: int, order_id: int) -> Order:
    order = await _get_locked(session, order_id)
    if order.user_id != user_id:
        raise RentalError("订单不存在")
    if order.wallet_state == "released":
        return order
    if order.status is not OrderStatus.RESERVED:
        raise RentalError("订单已进入采购,暂不能取消")
    await release_order(session, order, reason="USER_CANCELLED")
    return order
