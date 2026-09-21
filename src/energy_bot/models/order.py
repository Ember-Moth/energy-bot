"""能量租赁订单。"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from energy_bot.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from energy_bot.models.user import User


class OrderStatus(enum.StrEnum):
    """能量租赁订单状态:下单 → 收款/冻结 → 上游委托 → 完成/失败/退款。"""

    RESERVED = "reserved"  # 用户余额已冻结,等待采购
    DRAFT = "draft"  # 已创建待支付
    PAID = "paid"  # 已收款待采购
    DELEGATING = "delegating"  # 已提交上游,等待能量到账
    ACTIVE = "active"  # 能量已到账(成功终态;用户随即使用,不管理上游租期)
    FAILED = "failed"  # 上游执行失败
    REFUNDED = "refunded"  # 已退款


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index(
            "ix_orders_ready_queue",
            "next_run_at",
            "id",
            postgresql_where=text("next_run_at IS NOT NULL"),
        ),
        UniqueConstraint("user_id", "request_key", name="uq_order_request"),
        UniqueConstraint(
            "provider", "upstream_order_id", name="uq_orders_provider_upstream_order_id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 能量接收地址(租赁目标)
    recipient_address: Mapped[str] = mapped_column(String(64), index=True)
    energy_amount: Mapped[int] = mapped_column(Integer)  # 能量数量
    duration_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 租期(小时)
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

    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wallet_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    max_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    purchase_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)

    user: Mapped[User] = relationship(back_populates="orders")
