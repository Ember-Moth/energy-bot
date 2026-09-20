"""TronBid Quick Rent API 客户端。

协议要点(快照见 docs/upstream/quick-rent-v2.json):
- 鉴权仅 `Authorization: Bearer <api_key>`,无签名、无 webhook,终态全靠轮询;
- 报价与下单同为 POST + JSON;下单带 idempotency_key,重复提交返回同一订单
  (响应体 `duplicate: true` 表示幂等重放;同键不同载荷返回 409);
- 金额一律 TRX 十进制字符串(如 "3.200000"),解析为 Decimal,禁用浮点;
- 订单状态机:pending_payment → paid → delegating → delegated(成功),
  failed / expired / cancelled 为终态;未支付订单可取消或改付款地址;
- 本客户端不自动重试任何请求,超时/5xx 后的恢复纪律在调用方。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

import aiohttp

from energy_bot.config import TronbidSettings

_IDEMPOTENCY_RE = re.compile(r"^.{8,128}$", re.DOTALL)  # 规范仅约束长度 8-128
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class TronbidApiError(RuntimeError):
    """上游错误:code 为上游 error 字段(小写)或客户端兜底码(大写)HTTP_ERROR / INVALID_RESPONSE。"""

    def __init__(
        self,
        code: str,
        status: int | None = None,
        retry_after: int | None = None,
        message: str = "",
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        parts = [self.code]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.retry_after is not None:
            parts.append(f"retry_after={self.retry_after}s")
        return " ".join(parts)


class TronbidOrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    DELEGATING = "delegating"
    DELEGATED = "delegated"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


PaymentMode = Literal["onchain", "balance"]


@dataclass(frozen=True, slots=True)
class TronbidQuote:
    price_trx: Decimal
    available: bool
    save_percent: float | None
    expires_in_sec: int


@dataclass(frozen=True, slots=True)
class TronbidOrder:
    id: str
    status: TronbidOrderStatus
    payment_mode: PaymentMode
    amount_trx: Decimal
    energy_amount: int
    duration_minutes: int
    target_address: str
    effective_energy_amount: int | None
    pay_address: str | None
    payer_address: str | None
    expires_at: str | None
    qr_payload: str | None
    duplicate: bool
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class TronbidBalance:
    balance_trx: Decimal


def _trx(value: Any, field: str) -> Decimal:
    """TRX 金额字段:必须是非负十进制字符串;拒绝科学计数法、负数与 NaN/Infinity。"""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise TronbidApiError("INVALID_RESPONSE", message=f"TRX 字段非法:{field}={value!r}")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise TronbidApiError(
            "INVALID_RESPONSE", message=f"TRX 字段非法:{field}={value!r}"
        ) from exc
    exponent = amount.as_tuple().exponent
    # 正指数只在科学计数法("1e3" → exponent=3)中出现;NaN 比较恒为 False,not 兜底
    if not amount.is_finite() or not amount >= 0 or not isinstance(exponent, int) or exponent > 0:
        raise TronbidApiError("INVALID_RESPONSE", message=f"TRX 字段非法:{field}={value!r}")
    return amount


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_order(data: Any) -> TronbidOrder:
    try:
        return TronbidOrder(
            id=data["id"],
            status=TronbidOrderStatus(data["status"]),
            payment_mode=data["payment_mode"],
            amount_trx=_trx(data["amount_trx"], "amount_trx"),
            energy_amount=int(data["energy_amount"]),
            duration_minutes=int(data["duration_minutes"]),
            target_address=data["target_address"],
            effective_energy_amount=_opt_int(data.get("effective_energy_amount")),
            pay_address=_opt_str(data.get("pay_address")),
            payer_address=_opt_str(data.get("payer_address")),
            expires_at=_opt_str(data.get("expires_at")),
            qr_payload=_opt_str(data.get("qr_payload")),
            duplicate=bool(data.get("duplicate", False)),
            error_code=_opt_str(data.get("error_code")),
            error_message=_opt_str(data.get("error_message")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TronbidApiError("INVALID_RESPONSE", message=f"订单字段缺失或非法:{exc}") from exc


class TronbidClient:
    """持有一个 aiohttp 会话;close() 随宿主资源栈清理。"""

    def __init__(self, settings: TronbidSettings) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> TronbidClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _base(self) -> str:
        """校验并返回 base URL;仅回环地址允许 http(本地测试)。"""
        base = self._settings.base_url.rstrip("/")
        parsed = urlparse(base)
        host_ok = parsed.scheme == "https" or parsed.hostname in _LOOPBACK_HOSTS
        if not host_ok or not parsed.path.startswith("/api/") or parsed.username or parsed.fragment:
            raise ValueError("base_url 必须是 https:// 且路径以 /api/ 开头(仅回环地址允许 http)")
        return base

    async def _request(
        self, method: str, resource: str, *, data: Any = None, raw_body: bytes | None = None
    ) -> Any:
        method = method.upper()
        if method == "GET" and (data is not None or raw_body is not None):
            raise ValueError("GET 请求不能携带 body")
        url = f"{self._base()}/{resource.lstrip('/')}"
        body = (
            None
            if data is None
            else json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        )
        if raw_body is not None:
            body = raw_body
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._settings.timeout_seconds)
            )
        response = await self._session.request(
            method,
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
            },
            allow_redirects=False,
        )
        return await _parse_response(response)

    # --- 业务接口 ---

    async def create_quote(self, *, energy_amount: int, duration_minutes: int) -> TronbidQuote:
        data = await self._request(
            "POST",
            "quote",
            data={"energy_amount": energy_amount, "duration_minutes": duration_minutes},
        )
        try:
            expires_in_sec = int(data["expires_in_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TronbidApiError("INVALID_RESPONSE", message=f"报价字段缺失或非法:{exc}") from exc
        available = data.get("available")
        if not isinstance(available, bool):
            raise TronbidApiError(
                "INVALID_RESPONSE", message=f"报价字段缺失或非法:available={available!r}"
            )
        save_percent = data.get("save_percent")
        return TronbidQuote(
            price_trx=_trx(data.get("price_trx"), "price_trx"),
            available=available,
            save_percent=float(save_percent) if isinstance(save_percent, int | float) else None,
            expires_in_sec=expires_in_sec,
        )

    async def create_order(
        self,
        *,
        idempotency_key: str,
        target_address: str,
        energy_amount: int,
        duration_minutes: int,
        payment_mode: PaymentMode = "onchain",
        payer_address: str = "",
    ) -> TronbidOrder:
        """幂等下单:同键同载荷重放返回同一订单(duplicate=true),同键异载荷 409。"""
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ValueError("idempotency_key 长度须为 8-128")
        payload: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "target_address": target_address,
            "energy_amount": energy_amount,
            "duration_minutes": duration_minutes,
            "payment_mode": payment_mode,
        }
        if payer_address:
            payload["payer_address"] = payer_address
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return await self.submit_order(body)

    async def submit_order(self, body: bytes) -> TronbidOrder:
        """按原始载荷重放相同幂等请求,只使用余额支付的限制由采购层保证。"""
        return _parse_order(await self._request("POST", "orders", raw_body=body))

    async def get_order(self, order_id: str) -> TronbidOrder:
        return _parse_order(await self._request("GET", f"orders/{order_id}"))

    async def cancel_order(self, order_id: str) -> TronbidOrder:
        """取消未支付订单(已支付/进行中返回 409,错误沿 TronbidApiError 上抛)。"""
        return _parse_order(await self._request("POST", f"orders/{order_id}/cancel"))

    async def set_order_payer(self, order_id: str, *, payer_address: str) -> TronbidOrder:
        """为未支付订单设置链上付款地址。"""
        return _parse_order(
            await self._request(
                "POST", f"orders/{order_id}/set-payer", data={"payer_address": payer_address}
            )
        )

    async def get_balance(self) -> TronbidBalance:
        data = await self._request("GET", "balance")
        return TronbidBalance(balance_trx=_trx(data.get("balance_trx"), "balance_trx"))


async def _parse_response(response: aiohttp.ClientResponse) -> Any:
    """成功(2xx)须为 JSON 对象;错误取 error 字段作稳定码,不认识的一律 HTTP_ERROR。"""
    raw = await response.read()
    try:
        payload: Any = json.loads(raw) if raw else None
    except json.JSONDecodeError, UnicodeDecodeError:
        payload = None
    retry_after_raw = response.headers.get("Retry-After")
    retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else None
    if not 200 <= response.status < 300:
        code = payload.get("error") if isinstance(payload, dict) else None
        if not (isinstance(code, str) and _ERROR_CODE_RE.fullmatch(code)):
            code = "HTTP_ERROR"
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        raise TronbidApiError(code, response.status, retry_after, message)
    if not isinstance(payload, dict):
        raise TronbidApiError("INVALID_RESPONSE", status=response.status, retry_after=retry_after)
    return payload
