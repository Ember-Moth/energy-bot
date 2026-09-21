"""initial schema

Revision ID: c19001b41cbc
Revises:
Create Date: 2026-09-20 23:14:52.708952

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c19001b41cbc"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        "upstream_deliveries",
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_table(
        "upstream_throttles",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orders_blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "request_history",
            postgresql.ARRAY(sa.DateTime(timezone=True)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "order_history",
            postgresql.ARRAY(sa.DateTime(timezone=True)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("rate_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("first_name", sa.String(length=128), nullable=False),
        sa.Column("language_code", sa.String(length=16), nullable=False),
        sa.Column("tron_address", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "deposit_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("fiat_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("expected_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("receive_address", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED",
                "PAID",
                "EXPIRED",
                "FAILED",
                name="depositstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("block_transaction_id", sa.String(length=128), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
        sa.UniqueConstraint("trade_id"),
    )
    op.create_index(op.f("ix_deposit_orders_status"), "deposit_orders", ["status"], unique=False)
    op.create_index(op.f("ix_deposit_orders_user_id"), "deposit_orders", ["user_id"], unique=False)
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_address", sa.String(length=64), nullable=False),
        sa.Column("energy_amount", sa.Integer(), nullable=False),
        sa.Column("duration_hours", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RESERVED",
                "DRAFT",
                "PAID",
                "DELEGATING",
                "ACTIVE",
                "FAILED",
                "REFUNDED",
                name="orderstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("upstream_order_id", sa.String(length=128), nullable=True),
        sa.Column("upstream_txid", sa.String(length=128), nullable=True),
        sa.Column("delegated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("request_key", sa.String(length=128), nullable=True),
        sa.Column("wallet_state", sa.String(length=16), nullable=True),
        sa.Column("max_cost", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("purchase_cost", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "upstream_order_id", name="uq_orders_provider_upstream_order_id"
        ),
        sa.UniqueConstraint("user_id", "request_key", name="uq_order_request"),
    )
    op.create_index(op.f("ix_orders_next_run_at"), "orders", ["next_run_at"], unique=False)
    op.create_index(
        "ix_orders_ready_queue",
        "orders",
        ["next_run_at", "id"],
        unique=False,
        postgresql_where=sa.text("next_run_at IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_orders_recipient_address"), "orders", ["recipient_address"], unique=False
    )
    op.create_index(op.f("ix_orders_status"), "orders", ["status"], unique=False)
    op.create_index(
        op.f("ix_orders_upstream_order_id"), "orders", ["upstream_order_id"], unique=False
    )
    op.create_index(op.f("ix_orders_user_id"), "orders", ["user_id"], unique=False)
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
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
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "event", name="uq_order_notification"),
    )
    op.create_index(
        "ix_notifications_ready_queue",
        "order_notifications",
        ["next_run_at", "id"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL"),
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
        sa.Column("quoted_cost", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("actual_cost", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("upstream_order_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("submit_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_request_id", sa.Text(), nullable=True),
        sa.Column("last_raw_code", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
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
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(op.f("ix_wallet_entries_user_id"), "wallet_entries", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_wallet_entries_user_id"), table_name="wallet_entries")
    op.drop_table("wallet_entries")
    op.drop_index(op.f("ix_purchase_attempts_order_id"), table_name="purchase_attempts")
    op.drop_table("purchase_attempts")
    op.drop_index("ix_notifications_ready_queue", table_name="order_notifications")
    op.drop_table("order_notifications")
    op.drop_table("wallets")
    op.drop_index(op.f("ix_orders_user_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_upstream_order_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_status"), table_name="orders")
    op.drop_index(op.f("ix_orders_recipient_address"), table_name="orders")
    op.drop_index("ix_orders_ready_queue", table_name="orders")
    op.drop_index(op.f("ix_orders_next_run_at"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_deposit_orders_user_id"), table_name="deposit_orders")
    op.drop_index(op.f("ix_deposit_orders_status"), table_name="deposit_orders")
    op.drop_table("deposit_orders")
    op.drop_table("users")
    op.drop_table("upstream_throttles")
    op.drop_table("upstream_deliveries")
    op.drop_index(op.f("ix_upstream_cache_expires_at"), table_name="upstream_cache")
    op.drop_table("upstream_cache")
