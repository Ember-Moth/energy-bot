"""TRONow 订单终态回调接收端。

验签顺序(硬性,依 docs/upstream/tronow.md):
读原始字节 → 大小上限 → 时间窗 → 常数时间验签 → 解析 JSON →
delivery ID 持久化去重 → 行锁匹配订单 → 状态流转 → 落库后才 2xx。
回调会重复投递:除首次外的投递一律幂等应答 200。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import TronowSettings
from energy_bot.models import UpstreamDelivery
from energy_bot.services import rental
from energy_bot.services.rental import RentalError
from energy_bot.services.upstream.tronow import TronowOrderStatus

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024  # 回调 body 上限,防滥用
_TIMESTAMP_TOLERANCE_SECONDS = 300.0  # 时间窗 ±5 分钟
_ORDER_STATUS_TO_SUCCEEDED: dict[TronowOrderStatus, bool] = {
    TronowOrderStatus.SUCCESS: True,
    TronowOrderStatus.FAILED: False,
    # 非终态不出现在回调里;防御式映射:REVIEWING/CONFIRMING/PROCESSING 不流转
}


def verify_signature(
    secret: str,
    *,
    timestamp: str,
    delivery_id: str,
    event: str,
    signature: str,
    body: bytes,
) -> bool:
    """v1=hex(HMAC-SHA256(secret, ts "." delivery "." event "." body)),常数时间比较。"""
    if not timestamp or not delivery_id or not event or not signature.startswith("v1="):
        return False
    canonical = f"{timestamp}.{delivery_id}.{event}".encode() + b"." + body
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v1=" + expected, signature)


def parse_timestamp(header: str) -> float | None:
    try:
        return float(header)
    except ValueError:
        return None


class TronowWebhookView:
    """持有配置与会话工厂;aiohttp handler 形式注册到 app。"""

    def __init__(
        self,
        settings: TronowSettings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    def register(self, app: web.Application, path: str) -> None:
        app.router.add_post(path, self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        if not self._settings.webhook_secret:
            # 未配置密钥时拒绝一切回调,防止误把未验证的请求当真
            return web.json_response({"error": "webhook not configured"}, status=503)

        body = await request.read()
        if len(body) > _MAX_BODY_BYTES:
            return web.json_response({"error": "body too large"}, status=413)

        timestamp = request.headers.get("X-Lease-Timestamp", "")
        delivery_id = request.headers.get("X-Lease-Delivery", "")
        event = request.headers.get("X-Lease-Event", "")
        signature = request.headers.get("X-Lease-Signature", "")

        sent_at = parse_timestamp(timestamp)
        if sent_at is None or abs(time.time() - sent_at) > _TIMESTAMP_TOLERANCE_SECONDS:
            logger.warning("TRONow 回调时间戳越窗: ts=%r delivery=%s", timestamp, delivery_id)
            return web.json_response({"error": "stale timestamp"}, status=401)

        if not verify_signature(
            self._settings.webhook_secret,
            timestamp=timestamp,
            delivery_id=delivery_id,
            event=event,
            signature=signature,
            body=body,
        ):
            logger.warning("TRONow 回调验签失败: delivery=%s event=%s", delivery_id, event)
            return web.json_response({"error": "invalid signature"}, status=401)

        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid json"}, status=400)

        # 重复投递幂等应答:去重记录与业务流转在同一事务内,先到者生效
        async with self._session_factory() as session:
            if await self._already_seen(session, delivery_id):
                return web.json_response({"status": "duplicate"})
            order_view = await self._apply_event(session, event, payload)
            session.add(UpstreamDelivery(delivery_id=delivery_id, provider="tronow", event=event))
            await session.commit()

        logger.info("TRONow 回调已处理: delivery=%s event=%s", delivery_id, event)
        return web.json_response({"status": order_view})

    @staticmethod
    async def _already_seen(session: AsyncSession, delivery_id: str) -> bool:
        if not delivery_id:
            return False
        existing = await session.scalar(
            select(UpstreamDelivery.delivery_id).where(UpstreamDelivery.delivery_id == delivery_id)
        )
        return existing is not None

    async def _apply_event(self, session: AsyncSession, event: str, payload: dict[str, Any]) -> str:
        """匹配订单并流转;返回给 TRONow 的应答视图。

        防御式解析:payload schema 官方未定稿,字段缺失只记日志、不影响 2xx 应答
        (终态以轮询兜底,误 5xx 只会引来重复投递)。
        """
        data = payload.get("data")
        nested = data if isinstance(data, dict) else payload
        order_id = nested.get("order_id")
        status_raw = nested.get("status")
        client_order_id = nested.get("client_order_id")
        if not isinstance(order_id, str) or not order_id:
            logger.warning("TRONow 回调缺少 order_id: event=%s keys=%s", event, sorted(payload))
            return "ignored"

        succeeded = None
        if isinstance(status_raw, str):
            try:
                succeeded = _ORDER_STATUS_TO_SUCCEEDED.get(TronowOrderStatus(status_raw))
            except ValueError:
                succeeded = None
        if succeeded is None:
            logger.info("TRONow 回调事件不触发流转: event=%s status=%r", event, status_raw)
            return "ignored"

        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id=order_id)
        if order is None:
            # 可能先于本地落库到达;记录后让 TRONow 重投,或等待轮询兜底
            logger.warning(
                "TRONow 回调未匹配到订单: upstream_order_id=%s client_order_id=%r",
                order_id,
                client_order_id,
            )
            return "unmatched"

        try:
            await rental.handle_terminal_event(
                session, order, succeeded=succeeded, upstream_txid=nested.get("txid") or ""
            )
        except RentalError:
            logger.exception("TRONow 回调流转失败: order=%s", order.id)
            return "rejected"
        return "applied"


def register_tronow_webhook(
    app: web.Application,
    settings: TronowSettings,
    session_factory: async_sessionmaker[AsyncSession],
    path: str = "/upstream/tronow/webhook",
) -> None:
    TronowWebhookView(settings, session_factory).register(app, path)
