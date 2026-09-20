"""能量租赁订单。"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from energy_bot.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from energy_bot.models.user import User


class OrderStatus(enum.StrEnum):
    """能量租赁订单状态:下单 → 收款 → 上游委托 → 租期进行 → 终态。"""

    DRAFT = "draft"  # 已创建待支付
    PAID = "paid"  # 已收款待采购
    DELEGATING = "delegating"  # 已提交上游,等待能量到账
    ACTIVE = "active"  # 能量已到账,租期进行中
    EXPIRED = "expired"  # 租期结束
    FAILED = "failed"  # 上游执行失败
    REFUNDED = "refunded"  # 已退款


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "provider", "upstream_order_id", name="uq_orders_provider_upstream_order_id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 能量接收地址(租赁目标)
    recipient_address: Mapped[str] = mapped_column(String(64), index=True)
    energy_amount: Mapped[int] = mapped_column(Integer)  # 能量数量
    duration_hours: Mapped[int] = mapped_column(Integer)  # 租期(小时)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 6))  # 订单金额
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16),
        default=OrderStatus.DRAFT,
        index=True,
    )
    # 上游对接(外部能量供应商)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    upstream_order_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    upstream_txid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delegated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="orders")
