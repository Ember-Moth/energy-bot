"""GMPay(epusdt)收款网关协议层。

协议依据:epusdt 仓库 wiki/API.md + 源码核实(接入方案见 docs/gmpay-integration.md):
- 鉴权为 HMAC-SHA256 签名,参数规范化复刻网关 sign.go:参数名 ASCII 升序、
  `key=value` 以 `&` 拼接、剔除空值与 signature;数字按 Go
  `strconv.FormatFloat('f', -1, 64)` 等价格式化(本模块用 repr(float) 复刻);
- 下单 currency="trx" 时网关 coin==base 短路汇率 1,amount 即 TRX 数量;
- 回调无时间戳/nonce,防重放完全靠商户侧永久去重(见 web/gmpay.py);
- 本客户端不自动重试任何请求,恢复纪律在调用方。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import aiohttp

from energy_bot.config import GmpaySettings

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DECIMAL_PLAIN_RE = re.compile(r"^-?\d+(\.\d+)?$")  # 十进制,无科学计数法


# 支付币种与网络(GMPay 下单参数);仅支持 TRX(tron 网络原生转账)
PAY_TOKEN = "trx"  # noqa: S105  # 链上币种代号,非凭据
PAY_NETWORK = "tron"


class GmpayApiError(RuntimeError):
    """网关错误:code 为上游错误码(数字字符串)或客户端兜底码 HTTP_ERROR / INVALID_RESPONSE。"""

    def __init__(
        self,
        code: str,
        status: int | None = None,
        message: str = "",
        retry_after: int | None = None,
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


def _format_number(value: float) -> str:
    """复刻 Go strconv.FormatFloat('f', -1, 64):最短精确十进制,禁用科学计数法。

    Python repr(float) 同为最短精确表示,仅在指数形式上不同;网关金额远到不了
    科学计数法阈值(1e-6 / 1e21),遇到即拒绝,防止静默签出不同规范串。
    """
    text = repr(value)
    if "e" in text or "E" in text:
        raise ValueError(f"数字超出十进制表示范围:{value!r}")
    if text.endswith(".0"):
        return text[:-2]
    return text


def canonical_params(params: dict[str, Any]) -> str:
    """规范化签名串:剔除 signature 与空值,参数名 ASCII 升序,`key=value` 以 `&` 拼接。"""
    parts: list[str] = []
    for key in sorted(params):
        if key == "signature":
            continue
        value = params[key]
        if value is None:
            continue
        if isinstance(value, bool):  # bool 是 int 子类,必须先挡
            raise TypeError(f"布尔参数不参与签名:{key}")
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, float):
            text = _format_number(value)
        elif isinstance(value, str):
            text = value
        else:
            raise TypeError(f"不支持的签名参数类型:{key}={value!r}")
        if text == "":
            continue
        parts.append(f"{key}={text}")
    return "&".join(parts)


def sign_params(params: dict[str, Any], secret_key: str) -> str:
    """HMAC-SHA256(secret_key, canonical_params),小写 hex,与网关 sign.go 一致。"""
    canonical = canonical_params(params)
    return hmac.new(secret_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def verify_callback(payload: dict[str, Any], secret_key: str) -> bool:
    """常数时间校验回调签名;缺 signature 或类型非法一律 False。"""
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    try:
        expected = sign_params(payload, secret_key)
    except TypeError, ValueError:
        return False
    return hmac.compare_digest(expected, signature.lower())


def _amount(value: Any, field: str) -> Decimal:
    """金额字段:接受十进制数字/字符串,拒绝科学计数法、负数、NaN/Infinity。"""
    if isinstance(value, bool):
        raise GmpayApiError("INVALID_RESPONSE", message=f"金额字段非法:{field}={value!r}")
    if isinstance(value, int | float):
        value = repr(value)
    if not isinstance(value, str) or not _DECIMAL_PLAIN_RE.fullmatch(value.strip()):
        raise GmpayApiError("INVALID_RESPONSE", message=f"金额字段非法:{field}={value!r}")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise GmpayApiError("INVALID_RESPONSE", message=f"金额字段非法:{field}={value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise GmpayApiError("INVALID_RESPONSE", message=f"金额字段非法:{field}={value!r}")
    return amount


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class GmpayTransaction:
    trade_id: str
    order_id: str
    amount: Decimal  # 下单金额(currency=trx 时即 TRX 数量)
    actual_amount: Decimal  # 应付 TRX(currency=trx 时与 amount 相等)
    receive_address: str
    status: int
    expiration_time: int  # Unix 秒


class GmpayClient:
    """持有一个 aiohttp 会话;close() 随宿主资源栈清理。"""

    def __init__(self, settings: GmpaySettings) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> GmpayClient:
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
        if not host_ok or parsed.username or parsed.fragment:
            raise ValueError("base_url 必须是 https://(仅回环地址允许 http)")
        return base

    async def _post(self, resource: str, params: dict[str, Any]) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._settings.timeout_seconds)
            )
        url = f"{self._base()}/{resource.lstrip('/')}"
        body = json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode()
        response = await self._session.post(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            allow_redirects=False,
        )
        return await _parse_response(response)

    async def _get(self, resource: str) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._settings.timeout_seconds)
            )
        url = f"{self._base()}/{resource.lstrip('/')}"
        response = await self._session.get(url, allow_redirects=False)
        return await _parse_response(response)

    # --- 业务接口 ---

    async def create_transaction(
        self,
        *,
        order_id: str,
        amount: Decimal,
        notify_url: str,
        token: str = PAY_TOKEN,
        network: str = PAY_NETWORK,
        name: str = "",
    ) -> GmpayTransaction:
        """创建收款订单;currency=trx 时 amount 直接是 TRX 数量。"""
        if not order_id or len(order_id) > 32:
            raise ValueError("order_id 长度须为 1-32")
        if amount <= 0:
            raise ValueError("amount 必须为正数")
        params: dict[str, Any] = {
            "pid": self._settings.pid,
            "order_id": order_id,
            "currency": self._settings.currency,
            "token": token,
            "network": network,
            "amount": float(amount),
            "notify_url": notify_url,
            "name": name,
        }
        params["signature"] = sign_params(params, self._settings.secret_key)
        data = await self._post("/payments/gmpay/v1/order/create-transaction", params)
        try:
            return GmpayTransaction(
                trade_id=data["trade_id"],
                order_id=data["order_id"],
                amount=_amount(data.get("amount"), "amount"),
                actual_amount=_amount(data.get("actual_amount"), "actual_amount"),
                receive_address=data["receive_address"],
                status=int(data["status"]),
                expiration_time=int(data["expiration_time"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GmpayApiError(
                "INVALID_RESPONSE", message=f"下单响应字段缺失或非法:{exc}"
            ) from exc

    async def check_status(self, trade_id: str) -> int:
        """查询支付状态(无需签名):1 等待支付 / 2 成功 / 3 过期 / 4 待选网络。"""
        if not trade_id:
            raise ValueError("trade_id 不能为空")
        data = await self._get(f"/pay/check-status/{trade_id}")
        try:
            return int(data["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GmpayApiError("INVALID_RESPONSE", message=f"状态查询响应非法:{exc}") from exc


async def _parse_response(response: aiohttp.ClientResponse) -> Any:
    """网关统一 {code, data, msg} 信封:code=200 为成功,其余按错误上抛。"""
    raw = await response.read()
    try:
        payload: Any = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    retry_after_raw = response.headers.get("Retry-After")
    retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else None
    if not isinstance(payload, dict):
        raise GmpayApiError("HTTP_ERROR", status=response.status, retry_after=retry_after)
    code = payload.get("code")
    if code == 200:
        return payload.get("data")
    raw_msg: Any = payload.get("msg")
    message = raw_msg if isinstance(raw_msg, str) else ""
    raise GmpayApiError(
        str(code) if code is not None else "INVALID_RESPONSE",
        status=response.status,
        message=message,
        retry_after=retry_after,
    )
