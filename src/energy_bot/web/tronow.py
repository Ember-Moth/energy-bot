"""TRONow 终态回调:验签、校验载荷、确认租期、事务流转与持久化去重。

只有已完成业务处理的投递才确认送达。无效载荷、未匹配订单或查单失败
均返回非 2xx 且不保存 delivery,让上游能够重投。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientError, web
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from energy_bot.config import TronowSettings
from energy_bot.models import OrderStatus, UpstreamDelivery
from energy_bot.services import rental
from energy_bot.services.procurement import wake_tronow
from energy_bot.services.rental import RentalError
from energy_bot.services.upstream.tronow import TronowApiError, TronowClient, TronowOrderStatus

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024
_TIMESTAMP_TOLERANCE_SECONDS = 300
_EVENT_STATUS = {"order.succeeded": "SUCCESS", "order.failed": "FAILED"}


def verify_signature(
    secret: str,
    *,
    timestamp: str,
    delivery_id: str,
    event: str,
    signature: str,
    body: bytes,
) -> bool:
    """v1=hex(HMAC-SHA256(secret, ts "." delivery "." event "." body))。"""
    if not timestamp or not delivery_id or not event:
        return False
    if re.fullmatch(r"v1=[0-9a-f]{64}", signature) is None:
        return False
    canonical = f"{timestamp}.{delivery_id}.{event}".encode() + b"." + body
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v1=" + expected, signature)


def parse_timestamp(header: str) -> int | None:
    """按 Unix 秒解释回调时间戳;拒绝非整数、NaN、无穷与超长输入。"""
    if re.fullmatch(r"[0-9]{1,12}", header) is None:
        return None
    return int(header)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("租期时间必须是带时区的 ISO 8601 字符串")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("租期时间缺少时区")
    return parsed


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
            return web.json_response({"error": "webhook not configured"}, status=503)

        if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
            return web.json_response({"error": "body too large"}, status=413)
        # 分块读取限制实际分配,也覆盖未声明长度的请求。
        body = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > _MAX_BODY_BYTES:
                return web.json_response({"error": "body too large"}, status=413)
        raw = bytes(body)

        timestamp = request.headers.get("X-Lease-Timestamp", "")
        delivery_id = request.headers.get("X-Lease-Delivery", "")
        event = request.headers.get("X-Lease-Event", "")
        signature = request.headers.get("X-Lease-Signature", "")
        sent_at = parse_timestamp(timestamp)
        if sent_at is None or abs(time.time() - sent_at) > _TIMESTAMP_TOLERANCE_SECONDS:
            return web.json_response({"error": "stale timestamp"}, status=401)
        if not verify_signature(
            self._settings.webhook_secret,
            timestamp=timestamp,
            delivery_id=delivery_id,
            event=event,
            signature=signature,
            body=raw,
        ):
            return web.json_response({"error": "invalid signature"}, status=401)
        if len(delivery_id) > 128 or event not in _EVENT_STATUS:
            return web.json_response({"error": "invalid delivery or event"}, status=422)
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid json"}, status=400)

        async with self._session_factory() as session:
            if await self._already_seen(session, delivery_id):
                return web.json_response({"status": "duplicate"})
            order_view = await self._apply_event(session, event, payload)
            if order_view != "applied":
                status = 422 if order_view == "invalid" else 503
                return web.json_response({"status": order_view}, status=status)
            session.add(UpstreamDelivery(delivery_id=delivery_id, provider="tronow", event=event))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # 仅已持久化的同一投递可以确认重复,其他约束失败必须暴露。
                if not await self._already_seen(session, delivery_id):
                    raise
                return web.json_response({"status": "duplicate"})

        logger.info("TRONow 回调已处理: delivery=%s event=%s", delivery_id, event)
        return web.json_response({"status": order_view})

    @staticmethod
    async def _already_seen(session: AsyncSession, delivery_id: str) -> bool:
        existing = await session.scalar(
            select(UpstreamDelivery.delivery_id).where(UpstreamDelivery.delivery_id == delivery_id)
        )
        return existing is not None

    async def _lease_expiry(self, order_id: str, nested: dict[str, Any]) -> datetime:
        """优先使用签名载荷的租期时间;缺失时以只读订单查询补全。"""
        expires_at = _parse_datetime(nested.get("lease_expires_at"))
        if expires_at is not None:
            return expires_at
        confirmed_at = _parse_datetime(nested.get("confirmed_at"))
        if confirmed_at is None:
            async with TronowClient(self._settings) as client:
                snapshot = await client.get_order(order_id)
            if snapshot.order_id != order_id or snapshot.status is not TronowOrderStatus.SUCCESS:
                raise RentalError("查单结果尚未确认该订单成功")
            expires_at = _parse_datetime(snapshot.lease_expires_at)
            if expires_at is not None:
                return expires_at
            confirmed_at = _parse_datetime(snapshot.confirmed_at)
        if confirmed_at is None:
            raise RentalError("上游尚未返回租期时间")
        # TRONow 当前产品固定为 1h,不能按本地通知到达时间重新起算。
        return confirmed_at + timedelta(hours=1)

    async def _apply_event(self, session: AsyncSession, event: str, payload: dict[str, Any]) -> str:
        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            return "invalid"
        nested = data if isinstance(data, dict) else payload
        order_id = nested.get("order_id")
        expected = _EVENT_STATUS.get(event)
        if (
            expected is None
            or not isinstance(order_id, str)
            or not order_id
            or nested.get("status") != expected
            or ("event" in payload and payload["event"] != event)
        ):
            logger.warning("TRONow 回调载荷不符合终态契约: event=%s", event)
            return "invalid"

        if await wake_tronow(session, order_id, nested.get("client_order_id")):
            return "applied"

        order = await rental.get_by_upstream(session, provider="tronow", upstream_order_id=order_id)
        if order is None:
            return "unmatched"
        succeeded = expected == "SUCCESS"
        txid = nested.get("txid")
        try:
            expires_at = None
            if succeeded and order.status is OrderStatus.DELEGATING:
                expires_at = await self._lease_expiry(order_id, nested)
            await rental.handle_terminal_event(
                session,
                order,
                succeeded=succeeded,
                upstream_txid=txid if isinstance(txid, str) else "",
                lease_expires_at=expires_at,
            )
        except TypeError, ValueError, OverflowError, ClientError, TimeoutError, TronowApiError:
            # 不记录异常原文,避免 HTTP 异常携带凭据或不受控响应内容。
            logger.warning("TRONow 回调无法确认租期或完成流转: order=%s", order.id)
            return "retry"
        return "applied"


def register_tronow_webhook(
    app: web.Application,
    settings: TronowSettings,
    session_factory: async_sessionmaker[AsyncSession],
    path: str = "/upstream/tronow/webhook",
) -> None:
    TronowWebhookView(settings, session_factory).register(app, path)
