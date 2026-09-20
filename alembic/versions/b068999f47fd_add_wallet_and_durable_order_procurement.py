"""用户账本与持久化订单采购

Revision ID: b068999f47fd
Revises: 5578d0593605
Create Date: 2026-09-20 01:33:57.590938

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b068999f47fd"
down_revision: str | Sequence[str] | None = "5578d0593605"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """添加账本、采购尝试、通知与兼容旧订单的调度字段。"""
    op.create_table(
        "wallets",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("available", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("frozen", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("available >= 0", name="ck_wallet_available"),
        sa.CheckConstraint("frozen >= 0", name="ck_wallet_frozen"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "order_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "event", name="uq_order_notification"),
    )
    op.create_table(
        "purchase_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_body", sa.Text(), nullable=False),
        sa.Column("quoted_cost", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("actual_cost", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("upstream_order_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("order_id", "sequence", name="uq_purchase_sequence"),
        sa.UniqueConstraint("provider", "upstream_order_id", name="uq_purchase_upstream"),
    )
    op.create_index(
        op.f("ix_purchase_attempts_order_id"), "purchase_attempts", ["order_id"], unique=False
    )
    op.create_table(
        "wallet_entries",
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("available_delta", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("frozen_delta", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(op.f("ix_wallet_entries_user_id"), "wallet_entries", ["user_id"], unique=False)
    op.add_column("orders", sa.Column("duration_minutes", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("request_key", sa.String(length=128), nullable=True))
    op.add_column("orders", sa.Column("wallet_state", sa.String(length=16), nullable=True))
    op.add_column("orders", sa.Column("max_cost", sa.Numeric(precision=20, scale=6), nullable=True))
    op.add_column(
        "orders", sa.Column("purchase_cost", sa.Numeric(precision=20, scale=6), nullable=True)
    )
    op.add_column("orders", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("lease_token", sa.String(length=64), nullable=True))
    op.add_column("orders", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "orders", sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("orders", sa.Column("last_error", sa.String(length=128), nullable=True))
    op.alter_column("orders", "duration_hours", existing_type=sa.INTEGER(), nullable=True)
    op.create_index(op.f("ix_orders_next_run_at"), "orders", ["next_run_at"], unique=False)
    op.create_unique_constraint("uq_order_request", "orders", ["user_id", "request_key"])


def downgrade() -> None:
    """只允许空账本降级,防止丢失已发生的用户资金流水。"""
    entries = sa.table("wallet_entries", sa.column("key"))
    if op.get_bind().execute(sa.select(entries.c.key).limit(1)).first() is not None:
        raise RuntimeError("存在资金流水,禁止直接降级;请先备份并制定账本迁移方案")
    op.drop_constraint("uq_order_request", "orders", type_="unique")
    op.drop_index(op.f("ix_orders_next_run_at"), table_name="orders")
    op.alter_column("orders", "duration_hours", existing_type=sa.INTEGER(), nullable=False)
    op.drop_column("orders", "last_error")
    op.drop_column("orders", "retry_count")
    op.drop_column("orders", "lease_until")
    op.drop_column("orders", "lease_token")
    op.drop_column("orders", "next_run_at")
    op.drop_column("orders", "purchase_cost")
    op.drop_column("orders", "max_cost")
    op.drop_column("orders", "wallet_state")
    op.drop_column("orders", "request_key")
    op.drop_column("orders", "duration_minutes")
    op.drop_index(op.f("ix_wallet_entries_user_id"), table_name="wallet_entries")
    op.drop_table("wallet_entries")
    op.drop_index(op.f("ix_purchase_attempts_order_id"), table_name="purchase_attempts")
    op.drop_table("purchase_attempts")
    op.drop_table("order_notifications")
    op.drop_table("wallets")
