"""ORM 模型包:每类实体一个模块。

新增模型:在包内建模块(继承 `base.Base`,时间列用 `base.TimestampMixin`),
并在此导出——alembic autogenerate 依赖这里的 `Base.metadata` 汇总全部表,
漏导出就看不到对应的表。schema 变更流程见 AGENTS.md / docs/development.md。
"""

from energy_bot.models.base import Base, TimestampMixin
from energy_bot.models.order import Order, OrderStatus
from energy_bot.models.purchase import OrderNotification, PurchaseAttempt
from energy_bot.models.upstream_delivery import UpstreamDelivery
from energy_bot.models.user import User
from energy_bot.models.wallet import Wallet, WalletEntry

__all__ = [
    "Base",
    "Order",
    "OrderNotification",
    "OrderStatus",
    "PurchaseAttempt",
    "TimestampMixin",
    "UpstreamDelivery",
    "User",
    "Wallet",
    "WalletEntry",
]
