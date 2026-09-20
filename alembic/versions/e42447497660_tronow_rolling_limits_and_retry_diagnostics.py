"""TRONow 滚动窗口及采购重试诊断

Revision ID: e42447497660
Revises: ac2fb88df8cf
Create Date: 2026-09-20 04:08:06.774168

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e42447497660"
down_revision: str | Sequence[str] | None = "ac2fb88df8cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保留原限流字段并新增滚动历史、配额快照及重试元数据。"""
    op.add_column(
        "purchase_attempts",
        sa.Column("submit_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("purchase_attempts", sa.Column("last_http_status", sa.Integer(), nullable=True))
    op.add_column("purchase_attempts", sa.Column("last_request_id", sa.Text(), nullable=True))
    op.add_column("purchase_attempts", sa.Column("last_raw_code", sa.Text(), nullable=True))
    op.add_column(
        "upstream_throttles",
        sa.Column("orders_blocked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "upstream_throttles",
        sa.Column(
            "request_history",
            postgresql.ARRAY(sa.DateTime(timezone=True)),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "upstream_throttles",
        sa.Column(
            "order_history",
            postgresql.ARRAY(sa.DateTime(timezone=True)),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "upstream_throttles",
        sa.Column("rate_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "upstream_throttles", sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """移除新增诊断及窗口字段,订单和资金记录不变。"""
    op.drop_column("upstream_throttles", "snapshot_at")
    op.drop_column("upstream_throttles", "rate_snapshot")
    op.drop_column("upstream_throttles", "order_history")
    op.drop_column("upstream_throttles", "request_history")
    op.drop_column("upstream_throttles", "orders_blocked_until")
    op.drop_column("purchase_attempts", "last_raw_code")
    op.drop_column("purchase_attempts", "last_request_id")
    op.drop_column("purchase_attempts", "last_http_status")
    op.drop_column("purchase_attempts", "submit_attempts")
