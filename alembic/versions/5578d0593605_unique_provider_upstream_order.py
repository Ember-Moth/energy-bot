"""供应商内上游订单号唯一约束

Revision ID: 5578d0593605
Revises: dc2af768390e
Create Date: 2026-09-20 01:11:04.970707

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5578d0593605"
down_revision: str | Sequence[str] | None = "dc2af768390e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """添加唯一约束;已有重复绑定时让迁移失败,不得静默删除订单。"""
    op.create_unique_constraint(
        "uq_orders_provider_upstream_order_id", "orders", ["provider", "upstream_order_id"]
    )


def downgrade() -> None:
    """移除唯一约束。"""
    op.drop_constraint("uq_orders_provider_upstream_order_id", "orders", type_="unique")
