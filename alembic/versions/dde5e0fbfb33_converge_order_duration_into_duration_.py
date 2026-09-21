"""converge order duration into duration_minutes

Revision ID: dde5e0fbfb33
Revises: c19001b41cbc
Create Date: 2026-09-21 03:20:15.519112

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dde5e0fbfb33"
down_revision: str | Sequence[str] | None = "c19001b41cbc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先用 duration_hours 回填缺失的分钟数,再收窄非空、最后删列。
    op.execute(
        "UPDATE orders SET duration_minutes = duration_hours * 60 "
        "WHERE duration_minutes IS NULL AND duration_hours IS NOT NULL"
    )
    # 两个字段都为空的存量行是异常数据,显式报错而不是让非空约束抛出晦涩错误。
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM orders WHERE duration_minutes IS NULL) THEN "
        "RAISE EXCEPTION 'orders 存在无租期的存量行,需人工补数后再迁移'; "
        "END IF; END $$"
    )
    op.alter_column("orders", "duration_minutes", existing_type=sa.INTEGER(), nullable=False)
    op.drop_column("orders", "duration_hours")


def downgrade() -> None:
    op.add_column(
        "orders", sa.Column("duration_hours", sa.INTEGER(), autoincrement=False, nullable=True)
    )
    op.execute(
        "UPDATE orders SET duration_hours = duration_minutes / 60 WHERE duration_minutes % 60 = 0"
    )
    op.alter_column("orders", "duration_minutes", existing_type=sa.INTEGER(), nullable=True)
