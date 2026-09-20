"""ORM 模型包:每类实体一个模块。

新增模型:在包内建模块(继承 `base.Base`,时间列用 `base.TimestampMixin`),
并在此导出——alembic autogenerate 依赖这里的 `Base.metadata` 汇总全部表,
漏导出就看不到对应的表。schema 变更流程见 AGENTS.md / docs/development.md。
"""

from energy_bot.models.base import Base, TimestampMixin
from energy_bot.models.order import Order, OrderStatus
from energy_bot.models.user import User

__all__ = ["Base", "Order", "OrderStatus", "TimestampMixin", "User"]
