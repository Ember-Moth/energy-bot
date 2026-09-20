"""允许单上游直采没有预报价

Revision ID: 0153d2ad0e26
Revises: e42447497660
Create Date: 2026-09-20 04:44:31.816552

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0153d2ad0e26"
down_revision: str | Sequence[str] | None = "e42447497660"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保留已有报价,直采使用 NULL 表示未询价。"""
    op.alter_column(
        "purchase_attempts",
        "quoted_cost",
        existing_type=sa.NUMERIC(precision=20, scale=6),
        nullable=True,
    )


def downgrade() -> None:
    """已有 NULL 报价时拒绝收紧约束,不得用零或销售价伪造历史报价。"""
    op.alter_column(
        "purchase_attempts",
        "quoted_cost",
        existing_type=sa.NUMERIC(precision=20, scale=6),
        nullable=False,
    )
