"""用户 TRX 账本;充值渠道与上游账户独立。"""

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from energy_bot.models.base import Base, TimestampMixin


class Wallet(TimestampMixin, Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("available >= 0", name="ck_wallet_available"),
        CheckConstraint("frozen >= 0", name="ck_wallet_frozen"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    available: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal(0))
    frozen: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal(0))


class WalletEntry(TimestampMixin, Base):
    __tablename__ = "wallet_entries"

    # credit:外部凭证 / hold:订单号 / capture:订单号 / release:订单号;全局唯一。
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    available_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    frozen_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6))
