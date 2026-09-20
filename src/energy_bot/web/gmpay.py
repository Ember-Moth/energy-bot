"""GMPay 支付成功回调接收端。

纪律(与 TRONow 回调同源,协议差异见 docs/gmpay-integration.md):
大小上限 → 解析 JSON → 常数时间验签 → 行锁匹配充值单 →
金额币种校验 → 钱包入账 → 去重落库,同事务提交后才应答 ok。
epusdt 签名覆盖参数字典而非原始字节(先解析后验签),回调无时间戳,
防重放完全靠 delivery 去重;epusdt 只认 200 + 纯文本 ok/success。
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import GmpaySettings
from energy_bot.models import DepositStatus, UpstreamDelivery
from energy_bot.services import deposit as deposit_service
from energy_bot.services.deposit import DepositError
from energy_bot.services.payment.gmpay import verify_callback

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024  # 回调 body 上限,防滥用


def _ok() -> web.Response:
    """响应带有 writer/EOF 状态,每次应答必须新建,不能跨请求复用。"""
    return web.Response(text="ok")


class GmpayWebhookView:
    """持有配置与会话工厂;aiohttp handler 形式注册到 app。"""

    def __init__(
        self,
        settings: GmpaySettings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    def register(self, app: web.Application, path: str) -> None:
        app.router.add_post(path, self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        if not self._settings.secret_key:
            # 未配置密钥时拒绝一切回调,防止误把未验证的请求当真
            return web.Response(text="fail", status=503)

        declared = request.content_length
        if declared is not None and declared > _MAX_BODY_BYTES:
            return web.Response(text="fail", status=413)
        body = await request.read()
        if len(body) > _MAX_BODY_BYTES:
            return web.Response(text="fail", status=413)

        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(text="fail", status=400)
        if not isinstance(payload, dict):
            return web.Response(text="fail", status=400)

        if not verify_callback(payload, self._settings.secret_key):
            logger.warning("GMPay 回调验签失败: trade_id=%r", payload.get("trade_id"))
            return web.Response(text="fail", status=401)

        if payload.get("status") != 2:
            # 协议只回调支付成功(status=2);其他状态不处理但正常应答
            logger.info("GMPay 回调非成功状态: status=%r", payload.get("status"))
            return _ok()

        trade_id = payload.get("trade_id")
        if not isinstance(trade_id, str) or not trade_id:
            logger.warning("GMPay 回调缺少 trade_id: keys=%s", sorted(payload))
            return _ok()  # 无法定位,重投也无意义

        actual_amount = _parse_amount(payload.get("actual_amount"))
        token = payload.get("token")
        txid = payload.get("block_transaction_id")

        async with self._session_factory() as session:
            if await self._already_seen(session, trade_id):
                return _ok()
            deposit = await deposit_service.get_by_trade_id_for_update(session, trade_id)
            if deposit is None:
                # 回调先于本地落库(或单号对不上):应答 503 换重投
                logger.warning("GMPay 回调未匹配到充值单: trade_id=%s", trade_id)
                return web.Response(text="fail", status=503)
            try:
                await deposit_service.mark_paid(
                    session,
                    deposit,
                    actual_amount=actual_amount,
                    token=token if isinstance(token, str) else "",
                    block_transaction_id=txid if isinstance(txid, str) else "",
                )
            except DepositError:
                # 金额/币种不符已置 failed 转人工;落库并应答 ok,不再重投
                logger.exception("GMPay 回调入账拒绝: trade_id=%s", trade_id)
                session.add(
                    UpstreamDelivery(
                        delivery_id=f"gmpay:{trade_id}", provider="gmpay", event="order.paid"
                    )
                )
                await session.commit()
                return _ok()
            session.add(
                UpstreamDelivery(
                    delivery_id=f"gmpay:{trade_id}", provider="gmpay", event="order.paid"
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # 并发的相同 trade_id 抢先落库(主键冲突):入账幂等,按重复应答
                await session.rollback()
                return _ok()

        if deposit.status is DepositStatus.PAID:
            logger.info("GMPay 充值已入账: trade_id=%s order=%s", trade_id, deposit.order_id)
        return _ok()

    @staticmethod
    async def _already_seen(session: AsyncSession, trade_id: str) -> bool:
        existing = await session.scalar(
            select(UpstreamDelivery.delivery_id).where(
                UpstreamDelivery.delivery_id == f"gmpay:{trade_id}"
            )
        )
        return existing is not None


def _parse_amount(value: Any) -> Decimal:
    """回调金额解析;非法值归 0,随后必然与 expected_amount 不等而转人工。"""
    if isinstance(value, bool):
        return Decimal(0)
    if isinstance(value, int | float):
        value = repr(value)
    if not isinstance(value, str):
        return Decimal(0)
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return Decimal(0)
    return amount if amount.is_finite() and amount >= 0 else Decimal(0)


def register_gmpay_webhook(
    app: web.Application,
    settings: GmpaySettings,
    session_factory: async_sessionmaker[AsyncSession],
    path: str = "/payment/gmpay/notify",
) -> None:
    GmpayWebhookView(settings, session_factory).register(app, path)
