"""能量租赁:订单状态机与工作流。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
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

# 允许的状态跃迁;EXPIRED / REFUNDED 为终态不再迁出
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset({OrderStatus.PAID, OrderStatus.RESERVED}),
    OrderStatus.RESERVED: frozenset({OrderStatus.DELEGATING, OrderStatus.REFUNDED}),
    OrderStatus.PAID: frozenset({OrderStatus.DELEGATING, OrderStatus.REFUNDED}),
    OrderStatus.DELEGATING: frozenset(
        {OrderStatus.ACTIVE, OrderStatus.FAILED, OrderStatus.REFUNDED}
    ),
    OrderStatus.ACTIVE: frozenset({OrderStatus.EXPIRED}),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.FAILED: frozenset({OrderStatus.REFUNDED}),  # 上游失败后可退款
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


async def place_order(
    session: AsyncSession,
    *,
    user_id: int,
    recipient_address: str,
    energy_amount: int,
    duration_hours: int,
    price: Decimal,
) -> Order:
    """创建待支付订单(draft)。"""
    if not is_valid_tron_address(recipient_address):
        raise RentalError(f"无效的 TRON 地址:{recipient_address}")
    if energy_amount <= 0:
        raise RentalError("energy_amount 必须为正整数")
    if duration_hours <= 0:
        raise RentalError("duration_hours 必须为正整数")
    if not price.is_finite() or price <= 0:
        raise RentalError("price 必须为正数")
    return await order_repo.create_order(
        session,
        user_id=user_id,
        recipient_address=recipient_address,
        energy_amount=energy_amount,
        duration_hours=duration_hours,
        price=price,
    )


async def _get_locked(session: AsyncSession, order_id: int) -> Order:
    order = await order_repo.get_order_for_update(session, order_id)
    if order is None:
        raise RentalError(f"订单不存在:{order_id}")
    return order


async def mark_paid(session: AsyncSession, order_id: int) -> Order:
    """收款确认:draft → paid。"""
    order = await _get_locked(session, order_id)
    if order.wallet_state is not None:
        raise RentalError("余额订单必须通过采购结算流程流转")
    ensure_transition(order, OrderStatus.PAID)
    order.status = OrderStatus.PAID
    await session.flush()
    return order


async def start_delegation(
    session: AsyncSession,
    order_id: int,
    *,
    provider: str,
    upstream_order_id: str,
) -> Order:
    """已收款订单提交上游采购:paid → delegating,记录上游单号。"""
    if not provider or not upstream_order_id:
        raise RentalError("provider 与 upstream_order_id 均不能为空")
    order = await _get_locked(session, order_id)
    if order.wallet_state is not None:
        raise RentalError("余额订单必须通过采购结算流程流转")
    ensure_transition(order, OrderStatus.DELEGATING)
    order.status = OrderStatus.DELEGATING
    order.provider = provider
    order.upstream_order_id = upstream_order_id
    order.delegated_at = _now()
    await session.flush()
    return order


async def activate(
    session: AsyncSession,
    order_id: int,
    *,
    upstream_txid: str = "",
    lease_expires_at: datetime | None = None,
    confirmed_at: datetime | None = None,
) -> Order:
    """按上游到期时间或确认时间激活;迟到且已过期的订单立即流转到 expired。"""
    order = await _get_locked(session, order_id)
    if order.wallet_state is not None:
        raise RentalError("余额订单必须通过采购结算流程流转")
    ensure_transition(order, OrderStatus.ACTIVE)
    for value in (lease_expires_at, confirmed_at):
        if value is not None and value.utcoffset() is None:
            raise RentalError("上游租期时间必须包含时区")
    expires_at = lease_expires_at or order.expires_at
    if expires_at is None and confirmed_at is not None:
        expires_at = confirmed_at + timedelta(
            minutes=order.duration_minutes or (order.duration_hours or 0) * 60
        )
    if expires_at is None:
        raise RentalError("缺少上游租期时间,必须查单确认后再激活")
    order.status = OrderStatus.ACTIVE
    order.expires_at = expires_at
    if upstream_txid:
        order.upstream_txid = upstream_txid
    if expires_at <= _now():
        ensure_transition(order, OrderStatus.EXPIRED)
        order.status = OrderStatus.EXPIRED
    await session.flush()
    return order


async def expire(session: AsyncSession, order_id: int) -> Order:
    """租期结束:active → expired(到期回收的后台任务调用)。"""
    order = await _get_locked(session, order_id)
    ensure_transition(order, OrderStatus.EXPIRED)
    order.status = OrderStatus.EXPIRED
    await session.flush()
    return order


async def fail(session: AsyncSession, order_id: int) -> Order:
    """上游执行失败:delegating → failed。"""
    order = await _get_locked(session, order_id)
    if order.wallet_state is not None:
        raise RentalError("余额订单必须通过采购结算流程流转")
    ensure_transition(order, OrderStatus.FAILED)
    order.status = OrderStatus.FAILED
    await session.flush()
    return order


async def refund(session: AsyncSession, order_id: int) -> Order:
    """退款:paid / delegating / failed → refunded。"""
    order = await _get_locked(session, order_id)
    if order.wallet_state is not None:
        raise RentalError("余额订单必须通过采购结算流程流转")
    ensure_transition(order, OrderStatus.REFUNDED)
    order.status = OrderStatus.REFUNDED
    await session.flush()
    return order


# --- 上游事件编排(webhook / 轮询共用),按 provider + upstream_order_id 定位订单 ---


async def get_by_upstream(
    session: AsyncSession, *, provider: str, upstream_order_id: str
) -> Order | None:
    return await order_repo.get_by_upstream_order_id(
        session, upstream_order_id, provider=provider, for_update=True
    )


async def handle_terminal_event(
    session: AsyncSession,
    order: Order,
    *,
    succeeded: bool,
    upstream_txid: str = "",
    lease_expires_at: datetime | None = None,
    confirmed_at: datetime | None = None,
) -> Order:
    """应用上游终态事件;重复投递或已过终态时幂等返回,不抛错。

    - delegating → active / failed(正常路径);
    - 订单已在目标终态 → 幂等成功;
    - 其他状态(active / expired 等)说明状态被轮询先改过,保持现状并返回。
    """
    target = OrderStatus.ACTIVE if succeeded else OrderStatus.FAILED
    if order.status is target:
        return order
    if order.status is not OrderStatus.DELEGATING:
        return order  # 非期望状态:不流转,由调用方记日志
    if succeeded:
        return await activate(
            session,
            order.id,
            upstream_txid=upstream_txid,
            lease_expires_at=lease_expires_at,
            confirmed_at=confirmed_at,
        )
    return await fail(session, order.id)


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
        duration_hours=duration_minutes // 60 if duration_minutes % 60 == 0 else None,
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
    order.expires_at = None
    order.delegated_at = _now()
    await session.flush()


async def complete_purchase(
    session: AsyncSession,
    order: Order,
    *,
    cost: Decimal,
    expires_at: datetime | None,
    txid: str = "",
) -> None:
    """受订单行锁保护的采购成功与扣款;TronBid 可暂缺可信到期时间。"""
    if order.wallet_state == "captured":
        return
    if order.wallet_state != "held":
        raise RentalError("已释放的订单不能再次扣款")
    if expires_at is not None and expires_at.utcoffset() is None:
        raise RentalError("到期时间必须包含时区")
    ensure_transition(order, OrderStatus.ACTIVE)
    await wallet.settle(
        session, user_id=order.user_id, order_id=order.id, amount=order.price, capture=True
    )
    order.wallet_state = "captured"
    order.purchase_cost = cost
    order.status = OrderStatus.ACTIVE
    order.expires_at = expires_at
    order.upstream_txid = txid or None
    if expires_at is not None and expires_at <= _now():
        ensure_transition(order, OrderStatus.EXPIRED)
        order.status = OrderStatus.EXPIRED
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
