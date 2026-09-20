"""可丢失的上游读缓存与持久化商户限流时间窗。"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from energy_bot.models.base import Base


class UpstreamCache(Base):
    __tablename__ = "upstream_cache"
    __table_args__ = ({"prefixes": ["UNLOGGED"]},)

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    refresh_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refresh_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UpstreamThrottle(Base):
    __tablename__ = "upstream_throttles"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    orders_blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    request_history: Mapped[list[datetime]] = mapped_column(
        ARRAY(DateTime(timezone=True)), default=list, server_default="{}"
    )
    order_history: Mapped[list[datetime]] = mapped_column(
        ARRAY(DateTime(timezone=True)), default=list, server_default="{}"
    )
    rate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
