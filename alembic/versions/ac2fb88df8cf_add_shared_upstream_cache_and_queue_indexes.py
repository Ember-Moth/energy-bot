"""上游 UNLOGGED 缓存、商户限流与就绪队列索引

Revision ID: ac2fb88df8cf
Revises: b068999f47fd
Create Date: 2026-09-20 03:36:23.876345

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ac2fb88df8cf"
down_revision: str | Sequence[str] | None = "b068999f47fd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """缓存可重建,订单队列和资金表保持普通持久表。"""
    op.create_table(
        "upstream_cache",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token", sa.String(length=64), nullable=True),
        sa.Column("refresh_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key"),
        prefixes=["UNLOGGED"],
    )
    op.create_index(
        op.f("ix_upstream_cache_expires_at"), "upstream_cache", ["expires_at"], unique=False
    )
    op.create_table(
        "upstream_throttles",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope"),
    )
    op.create_index(
        "ix_notifications_ready_queue",
        "order_notifications",
        ["next_run_at", "id"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_index(
        "ix_orders_ready_queue",
        "orders",
        ["next_run_at", "id"],
        unique=False,
        postgresql_where=sa.text("next_run_at IS NOT NULL"),
    )


def downgrade() -> None:
    """删除缓存及调度优化,不修改订单或资金数据。"""
    op.drop_index(
        "ix_orders_ready_queue",
        table_name="orders",
        postgresql_where=sa.text("next_run_at IS NOT NULL"),
    )
    op.drop_index(
        "ix_notifications_ready_queue",
        table_name="order_notifications",
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.drop_table("upstream_throttles")
    op.drop_index(op.f("ix_upstream_cache_expires_at"), table_name="upstream_cache")
    op.drop_table("upstream_cache")
