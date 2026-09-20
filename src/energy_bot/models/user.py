"""用户。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from energy_bot.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from energy_bot.models.order import Order


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram 用户 ID
    first_name: Mapped[str] = mapped_column(String(128), default="")
    language_code: Mapped[str] = mapped_column(String(16), default="")
    # 常用租赁收款地址(TBase1 开头),用户可后续绑定
    tron_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    orders: Mapped[list[Order]] = relationship(back_populates="user")
