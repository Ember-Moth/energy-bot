"""TRONow HTTP 元数据;配额快照用于观测和保守上界,不是可分配令牌。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass


def integer_header(value: str | None) -> int | None:
    if value is None or re.fullmatch(r"[0-9]{1,10}", value) is None:
        return None
    return int(value)


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    request_limit: int | None = None
    request_remaining: int | None = None
    order_limit: int | None = None
    order_remaining: int | None = None
    scope: str | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimitSnapshot:
        scope = headers.get("X-RateLimit-Scope")
        return cls(
            request_limit=integer_header(headers.get("X-RateLimit-Limit")),
            request_remaining=integer_header(headers.get("X-RateLimit-Remaining")),
            order_limit=integer_header(headers.get("X-OrderRateLimit-Limit")),
            order_remaining=integer_header(headers.get("X-OrderRateLimit-Remaining")),
            scope=scope if scope in ("requests", "orders") else None,
        )
