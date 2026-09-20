"""TRONow 商户 OpenAPI 客户端。

协议要点(完整要求见 docs/upstream/tronow.md):
- 签名为 7 行 LF 拼接的规范串做 HMAC-SHA256,JSON 只序列化一次,签名与发送同一份字节;
- 重试纪律在调用方:同一业务 ID + 同 body + 同幂等键,换新的时间戳/nonce/签名;
  超时/5xx 不代表失败,先按 client_order_id 查单;
- 金额一律 SUN 整数(1 TRX = 10^6 SUN),禁用浮点;
- 本客户端不自动重试任何请求。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

import aiohttp

from energy_bot.config import TronowSettings
from energy_bot.services.upstream.metadata import RateLimitSnapshot, integer_header
from energy_bot.services.upstream_gate import UpstreamGate

logger = logging.getLogger(__name__)

_CLIENT_ORDER_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{8,128}$")
_ENVELOPE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class TronowApiError(RuntimeError):
    """上游错误:code 为稳定业务码或客户端兜底码(HTTP_ERROR / INVALID_RESPONSE)。"""

    def __init__(
        self,
        code: str,
        status: int | None = None,
        request_id: str | None = None,
        retry_after: int | None = None,
        message: str = "",
        *,
        raw_code: str | None = None,
        rate_limits: RateLimitSnapshot | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.retry_after = retry_after
        self.raw_code = (
            raw_code
            if raw_code is not None
            else (code if code not in ("HTTP_ERROR", "INVALID_RESPONSE") else None)
        )
        self.rate_limits = rate_limits or RateLimitSnapshot()

    def __str__(self) -> str:
        parts = [self.code]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.retry_after is not None:
            parts.append(f"retry_after={self.retry_after}s")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class TronowResponse:
    data: Any
    status: int
    request_id: str | None
    retry_after: int | None
    location: str | None
    rate_limits: RateLimitSnapshot


def _log_error(error: TronowApiError) -> TronowApiError:
    logger.warning(
        "TRONow 请求失败: status=%s code=%r request_id=%r scope=%s",
        error.status,
        error.raw_code or error.code,
        error.request_id,
        error.rate_limits.scope,
    )
    return error


def _invalid_data(response: TronowResponse) -> TronowApiError:
    return _log_error(
        TronowApiError(
            "INVALID_RESPONSE",
            response.status,
            response.request_id,
            response.retry_after,
            raw_code="OK",
            rate_limits=response.rate_limits,
        )
    )


def _query_encode(value: str) -> str:
    """RFC 3986 严格转义(等价 Go url.QueryEscape):空格转 +,!'()* 也转义。"""
    return quote(value, safe="").replace("%20", "+")


def canonical_query(query: str) -> str:
    """规范查询串:键按字节排序(稳定,同键保持原值顺序),重复键保留。"""
    pairs = parse_qsl(query, keep_blank_values=True)
    ordered = sorted(pairs, key=lambda pair: pair[0].encode())
    return "&".join(f"{_query_encode(k)}={_query_encode(v)}" for k, v in ordered)


def sign(
    secret: str,
    *,
    method: str,
    path: str,
    query: str,
    timestamp: str,
    nonce: str,
    idempotency_key: str,
    body: bytes = b"",
) -> str:
    """对 7 行规范串签名;无结尾换行,读操作的幂等键为空行。"""
    canonical = "\n".join(
        (
            method.upper(),
            path,
            canonical_query(query),
            timestamp,
            nonce,
            idempotency_key.strip(),
            hashlib.sha256(body).hexdigest(),
        )
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


class TronowOrderStatus(StrEnum):
    PROCESSING = "PROCESSING"
    CONFIRMING = "CONFIRMING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REVIEWING = "REVIEWING"


@dataclass(frozen=True, slots=True)
class TronowQuote:
    resource_amount: int
    duration: str
    price_sun: int
    currency: str
    priced_at: str
    valid_until: datetime | None = None  # 本地读缓存的有效期,不是上游锁价


@dataclass(frozen=True, slots=True)
class TronowOrderAccepted:
    order_id: str
    client_order_id: str
    status: TronowOrderStatus
    reserved_amount_sun: int
    currency: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TronowOrder:
    order_id: str
    client_order_id: str
    status: TronowOrderStatus
    receiver_address: str
    resource_amount: int
    duration: str
    amount_sun: int
    currency: str
    txid: str | None
    failure_code: str | None
    created_at: str
    confirmed_at: str | None
    lease_expires_at: str | None
    request_id: str | None = None
    retry_after: int | None = None
    http_status: int = 200


@dataclass(frozen=True, slots=True)
class TronowBalance:
    currency: str
    available_balance_sun: int
    reserved_balance_sun: int
    total_balance_sun: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CreatedOrder:
    accepted: TronowOrderAccepted
    request_id: str | None
    retry_after: int | None
    http_status: int = 201


def _sun(value: Any, field: str) -> int:
    """SUN 金额字段:必须是非空十进制整数字符串。"""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None:
        return int(value)
    raise TronowApiError("INVALID_RESPONSE", message=f"SUN 字段非法:{field}={value!r}")


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _parse_order(response: TronowResponse) -> TronowOrder:
    data = response.data
    try:
        return TronowOrder(
            order_id=data["order_id"],
            client_order_id=data["client_order_id"],
            status=TronowOrderStatus(data["status"]),
            receiver_address=data["receiver_address"],
            resource_amount=int(data["resource_amount"]),
            duration=data["duration"],
            amount_sun=_sun(data["amount_sun"], "amount_sun"),
            currency=data["currency"],
            txid=_opt_str(data.get("txid")),
            failure_code=_opt_str(data.get("failure_code")),
            created_at=data["created_at"],
            confirmed_at=_opt_str(data.get("confirmed_at")),
            lease_expires_at=_opt_str(data.get("lease_expires_at")),
            request_id=response.request_id,
            retry_after=response.retry_after,
            http_status=response.status,
        )
    except (KeyError, TypeError, ValueError, TronowApiError) as exc:
        raise _invalid_data(response) from exc


class TronowClient:
    """持有一个 aiohttp 会话;close() 随宿主资源栈清理。"""

    def __init__(self, settings: TronowSettings, *, gate: UpstreamGate | None = None) -> None:
        self._settings = settings
        self._gate = gate
        self.last_rate_limits = RateLimitSnapshot()
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> TronowClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _base(self) -> tuple[str, str]:
        """校验并返回 (base, path 前缀);仅回环地址允许 http(本地测试)。"""
        base = self._settings.base_url.rstrip("/")
        parsed = urlparse(base)
        host_ok = parsed.scheme == "https" or parsed.hostname in _LOOPBACK_HOSTS
        if (
            not host_ok
            or not parsed.path.startswith("/openapi/v1")
            or parsed.username
            or parsed.fragment
        ):
            raise ValueError(
                "base_url 必须是 https:// 且路径以 /openapi/v1 开头(仅回环地址允许 http)"
            )
        return base, parsed.path

    async def _request(self, method: str, resource: str, **kwargs: Any) -> TronowResponse:
        if self._gate is None:
            return await self._request_unlimited(method, resource, **kwargs)
        return await self._gate.run(
            lambda: self._request_unlimited(method, resource, **kwargs),
            creation=method.upper() == "POST"
            and resource.split("?", 1)[0].strip("/") in ("orders", "address-activations"),
        )

    async def _request_unlimited(
        self,
        method: str,
        resource: str,
        *,
        data: Any = None,
        params: dict[str, str] | None = None,
        idempotency_key: str = "",
        raw_body: bytes | None = None,
    ) -> TronowResponse:
        method = method.upper()
        if method == "POST" and not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ValueError("POST 需要 8-128 位可见 ASCII 的稳定幂等键")
        if method == "GET" and (data is not None or raw_body is not None):
            raise ValueError("GET 请求不能携带 body")

        base, _ = self._base()
        path = f"{base}/{resource.lstrip('/')}"
        if params:
            query = "&".join(f"{_query_encode(k)}={_query_encode(v)}" for k, v in params.items())
            url = f"{path}?{query}"
        else:
            query = ""
            url = path

        body = (
            b""
            if data is None
            else json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        )
        if raw_body is not None:
            body = raw_body
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(18)  # 36 位,满足 16-128 位 URL-safe 要求
        parsed = urlparse(url)
        signature = sign(
            self._settings.api_secret,
            method=method,
            path=parsed.path,
            query=parsed.query,
            timestamp=timestamp,
            nonce=nonce,
            idempotency_key=idempotency_key,
            body=body,
        )
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._settings.api_key,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._settings.timeout_seconds)
            )
            # 禁止 aiohttp 复用旧鉴权头内部重试;队列负责计额并生成新的签名材料。
            self._session._retry_connection = False
        try:
            async with self._session.request(
                method,
                url,
                data=body if method == "POST" else None,
                headers=headers,
                allow_redirects=False,
            ) as response:
                snapshot = RateLimitSnapshot.from_headers(response.headers)
                self.last_rate_limits = snapshot
                try:
                    return await _parse_response(response, snapshot)
                finally:
                    if self._gate is not None:
                        try:
                            await self._gate.observe(snapshot)
                        except Exception as exc:
                            # 观测失败不能覆盖已受理订单的真实 HTTP 结果。
                            logger.warning("TRONow 配额快照保存失败: %s", type(exc).__name__)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise _log_error(TronowApiError("HTTP_ERROR")) from exc

    # --- 业务接口 ---

    async def get_quote(self, resource_amount: int) -> TronowQuote:
        response = await self._request(
            "GET",
            "prices/quote",
            params={
                "resource_type": "ENERGY",
                "resource_amount": str(resource_amount),
                "duration": "1h",
            },
        )
        data = response.data
        try:
            return TronowQuote(
                resource_amount=int(data["resource_amount"]),
                duration=data["duration"],
                price_sun=_sun(data["price_sun"], "price_sun"),
                currency=data["currency"],
                priced_at=data["priced_at"],
            )
        except (KeyError, TypeError, ValueError, TronowApiError) as exc:
            raise _invalid_data(response) from exc

    async def create_order(
        self,
        *,
        client_order_id: str,
        receiver_address: str,
        resource_amount: int,
        idempotency_key: str | None = None,
    ) -> CreatedOrder:
        """幂等下单:可显式传幂等键;默认保留长业务号,短业务号使用固定摘要。"""
        if not _CLIENT_ORDER_RE.fullmatch(client_order_id):
            raise ValueError("client_order_id 须为 1-64 位可见 ASCII")
        if idempotency_key is None:
            idempotency_key = (
                client_order_id
                if len(client_order_id) >= 8
                else "energy-bot:" + hashlib.sha256(client_order_id.encode()).hexdigest()
            )
        payload = {
            "client_order_id": client_order_id,
            "resource_type": "ENERGY",
            "receiver_address": receiver_address,
            "resource_amount": resource_amount,
            "duration": "1h",
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return await self.submit_order(body, idempotency_key=idempotency_key)

    async def submit_order(self, body: bytes, *, idempotency_key: str) -> CreatedOrder:
        """发送已持久化的精确请求字节;恢复时不得重新生成业务标识或载荷。"""
        response = await self._request(
            "POST", "orders", raw_body=body, idempotency_key=idempotency_key
        )
        data = response.data
        try:
            accepted = TronowOrderAccepted(
                order_id=data["order_id"],
                client_order_id=data["client_order_id"],
                status=TronowOrderStatus(data["status"]),
                reserved_amount_sun=_sun(data["reserved_amount_sun"], "reserved_amount_sun"),
                currency=data["currency"],
                created_at=data["created_at"],
            )
        except (KeyError, TypeError, ValueError, TronowApiError) as exc:
            raise _invalid_data(response) from exc
        return CreatedOrder(
            accepted=accepted,
            request_id=response.request_id,
            retry_after=response.retry_after,
            http_status=response.status,
        )

    async def get_order(self, order_id: str) -> TronowOrder:
        response = await self._request("GET", f"orders/{order_id}")
        return _parse_order(response)

    async def get_order_by_client_id(self, client_order_id: str) -> TronowOrder:
        response = await self._request("GET", "orders", params={"client_order_id": client_order_id})
        return _parse_order(response)

    async def get_balance(self) -> TronowBalance:
        response = await self._request("GET", "account/balance")
        data = response.data
        try:
            return TronowBalance(
                currency=data["currency"],
                available_balance_sun=_sun(data["available_balance_sun"], "available_balance_sun"),
                reserved_balance_sun=_sun(data["reserved_balance_sun"], "reserved_balance_sun"),
                total_balance_sun=_sun(data["total_balance_sun"], "total_balance_sun"),
                updated_at=data["updated_at"],
            )
        except (KeyError, TypeError, ValueError, TronowApiError) as exc:
            raise _invalid_data(response) from exc


async def _parse_response(
    response: aiohttp.ClientResponse,
    snapshot: RateLimitSnapshot | None = None,
) -> TronowResponse:
    """解析 JSON 或网关错误,所有异常保留原 HTTP 元数据,不按 message 分支。"""
    snapshot = snapshot or RateLimitSnapshot.from_headers(response.headers)
    request_id = response.headers.get("X-Request-ID")
    retry_after = integer_header(response.headers.get("Retry-After"))
    try:
        raw = await response.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise _log_error(
            TronowApiError(
                "HTTP_ERROR", response.status, request_id, retry_after, rate_limits=snapshot
            )
        ) from exc
    try:
        envelope: Any = json.loads(raw) if raw else None
    except json.JSONDecodeError, UnicodeDecodeError:
        envelope = None
    raw_code = envelope.get("code") if isinstance(envelope, dict) else None
    raw_code = raw_code if isinstance(raw_code, str) else None
    structured = raw_code is not None and bool(_ENVELOPE_CODE_RE.fullmatch(raw_code))
    if (
        isinstance(envelope, dict)
        and isinstance(envelope.get("request_id"), str)
        and envelope["request_id"]
    ):
        request_id = envelope["request_id"]
    ok = 200 <= response.status < 300
    if not (ok and structured and raw_code == "OK" and "data" in envelope):
        code = (
            (raw_code if structured and raw_code != "OK" else "HTTP_ERROR")
            if not ok
            else "INVALID_RESPONSE"
        )
        assert code is not None
        raise _log_error(
            TronowApiError(
                code,
                response.status,
                request_id,
                retry_after,
                raw_code=raw_code,
                rate_limits=snapshot,
            )
        )
    return TronowResponse(
        envelope["data"],
        response.status,
        request_id,
        retry_after,
        response.headers.get("Location"),
        snapshot,
    )
