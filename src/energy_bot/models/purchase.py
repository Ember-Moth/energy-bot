"""持久化采购请求与通知,进程退出后可恢复。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from energy_bot.models.base import Base, TimestampMixin


class PurchaseAttempt(TimestampMixin, Base):
    __tablename__ = "purchase_attempts"
    __table_args__ = (
        UniqueConstraint("order_id", "sequence", name="uq_purchase_sequence"),
        UniqueConstraint("provider", "upstream_order_id", name="uq_purchase_upstream"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    business_id: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_body: Mapped[str] = mapped_column(Text)  # 精确 JSON 字节的 UTF-8 文本
    quoted_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )  # 直采未询价为空
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    upstream_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="submitting")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submit_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_raw_code: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderNotification(TimestampMixin, Base):
    __tablename__ = "order_notifications"
    __table_args__ = (
        UniqueConstraint("order_id", "event", name="uq_order_notification"),
        Index(
            "ix_notifications_ready_queue",
            "next_run_at",
            "id",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    event: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
