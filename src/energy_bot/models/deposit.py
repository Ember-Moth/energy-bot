"""GMPay 充值单。"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from energy_bot.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from energy_bot.models.user import User


class DepositStatus(enum.StrEnum):
    """充值单状态:下单 → 网关确认到账 / 过期 / 异常。"""

    CREATED = "created"  # 已下单待支付
    PAID = "paid"  # 已到账并入账
    EXPIRED = "expired"  # 网关侧过期未支付
    FAILED = "failed"  # 金额/币种不符等异常,转人工核对


class DepositOrder(TimestampMixin, Base):
    """GMPay 充值单:入账经 wallet.credit(reference=f"gmpay:{trade_id}") 幂等。"""

    __tablename__ = "deposit_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 商户单号(dep- 前缀),全平台唯一
    order_id: Mapped[str] = mapped_column(String(32), unique=True)
    # GMPay 平台单号;回调对账键,下单失败时为空
    trade_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # 下单金额(currency=trx 时即用户输入的 TRX 数量)
    fiat_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    # 应付 TRX(下单响应 actual_amount,展示给用户;入账以它为准)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    receive_address: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[DepositStatus] = mapped_column(
        Enum(DepositStatus, native_enum=False, length=16),
        default=DepositStatus.CREATED,
        index=True,
    )
    block_transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()
