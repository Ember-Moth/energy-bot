"""上游回调投递去重。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from energy_bot.models.base import Base


class UpstreamDelivery(Base):
    """按投递 ID 持久化去重:同一 ID 重复投递直接幂等应答,业务只处理一次。"""

    __tablename__ = "upstream_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="tronow")
    event: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
