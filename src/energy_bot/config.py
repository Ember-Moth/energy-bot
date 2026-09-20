"""配置加载。

默认从平台用户配置目录读取 ``config.yaml``;``--config`` 参数或 ``ENERGY_BOT_CONFIG``
环境变量可指定其他路径。环境变量(前缀 ``ENERGY_BOT_``)优先级高于 YAML 文件,
嵌套键用双下划线(如 ``ENERGY_BOT_WEBHOOK__PORT``),方便注入密钥而不提交到代码库。
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from platformdirs import user_config_dir
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy import URL

DEFAULT_CONFIG_NAME = "config.yaml"
CONFIG_ENV = "ENERGY_BOT_CONFIG"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
TIMEZONE = ZoneInfo("Asia/Shanghai")  # 项目与数据库统一时区(东八区)


def default_config_path() -> Path:
    """按平台约定解析用户配置目录中的配置文件。

    macOS: ~/Library/Application Support/energy-bot/config.yaml
    Linux: ~/.config/energy-bot/config.yaml
    """
    return Path(user_config_dir("energy-bot")) / DEFAULT_CONFIG_NAME


def resolve_config_path(explicit: Path | None = None) -> Path:
    """优先级:--config 参数 > ENERGY_BOT_CONFIG 环境变量 > 平台默认路径。"""
    if explicit is not None:
        return explicit
    from_env = os.environ.get(CONFIG_ENV)
    if from_env:
        return Path(from_env)
    return default_config_path()


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_WEBHOOK_")

    base_url: str = ""  # 公网 HTTPS 基地址,更新会 POST 到 {base_url}{path}
    host: str = "127.0.0.1"  # 本地监听地址(通常前面有反向代理;需对外暴露可改 0.0.0.0)
    port: int = Field(default=8080, ge=1, le=65535)
    path: str = "/webhook"
    # Telegram 通过 X-Telegram-Bot-Api-Secret-Token 头携带;为空则每次启动自动生成
    secret_token: str = ""

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        # 允许留空:迁移等场景只需要 database 段,必填在启动时检查
        value = value.strip().rstrip("/")
        if value and not value.startswith("https://"):
            raise ValueError("必须是 https:// 开头的公网地址,如 https://bot.example.com")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = value.strip() or "/webhook"
        return value if value.startswith("/") else f"/{value}"


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_DATABASE_")

    address: str = ""  # 主机
    port: int = Field(default=5432, ge=1, le=65535)
    username: str = ""
    # 建议用环境变量注入,不落盘:ENERGY_BOT_DATABASE__PASSWORD
    password: str = ""
    database: str = ""
    pool_size: int = Field(default=5, ge=1)  # 连接池常驻连接数
    max_overflow: int = Field(default=10, ge=0)  # 峰值时允许的超额连接数
    # 完整连接串,只允许环境变量(ENERGY_BOT_DATABASE__DSN),设置后优先生效;
    # 配置文件里禁止出现 dsn 字段(load_settings 会拒绝)
    dsn: str = ""

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("postgresql://", "postgres://")):
            raise ValueError("必须是 postgresql:// 开头的连接串")
        return value

    def effective_dsn(self) -> str:
        """环境变量 DSN 优先;否则由离散字段拼装(用户名/密码自动转义)。"""
        if self.dsn:
            return self.dsn
        if not (self.address and self.username and self.database):
            raise ValueError(
                "需配置 address/username/database,"
                "或用环境变量 ENERGY_BOT_DATABASE__DSN 提供完整连接串"
            )
        return URL.create(
            "postgresql",
            username=self.username,
            password=self.password or None,
            host=self.address,
            port=self.port,
            database=self.database,
        ).render_as_string(hide_password=False)


class TronowSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_TRONOW_")

    base_url: str = "https://api.tronow.io/openapi/v1"
    api_key: str = ""  # 商户 API Key;建议环境变量注入不落盘
    api_secret: str = ""  # 请求签名密钥;与 webhook 密钥是两个独立密钥
    webhook_secret: str = ""  # 回调验签密钥(X-Lease-Signature);建议环境变量注入不落盘
    account_scope: str = ""  # 空时所有 TRONow key 共用默认商户桶
    request_limit: int = Field(default=50, ge=1, le=1000)
    order_limit: int = Field(default=10, ge=1, le=1000)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)  # 单请求超时


class TronbidSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_TRONBID_")

    base_url: str = "https://tronbid.com/api/v2/quick-rent"
    api_key: str = ""  # API Key(Authorization: Bearer);建议环境变量注入不落盘
    account_scope: str = ""  # 同一商户使用多个 API key 时配置相同限流标识
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)  # 单请求超时


class UpstreamSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_UPSTREAM_")

    tronow: TronowSettings = Field(default_factory=TronowSettings)
    tronbid: TronbidSettings = Field(default_factory=TronbidSettings)


class GmpaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_GMPAY_")

    base_url: str = ""  # 自部署 epusdt 实例地址;空 = 收款功能未启用
    pid: str = "1000"  # 商户 PID(参与签名)
    secret_key: str = ""  # 签名密钥;建议环境变量注入不落盘
    currency: str = "trx"  # 下单币种:trx = 金额即 TRX 数量(网关 coin==base 短路,汇率 1)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)  # 单请求超时


class PaymentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_PAYMENT_")

    gmpay: GmpaySettings = Field(default_factory=GmpaySettings)


class RentalProduct(BaseModel):
    energy_amount: int = Field(gt=0, le=2147483647)
    duration_minutes: int = Field(gt=0, le=525600)
    price_trx: Decimal = Field(gt=0, max_digits=12, decimal_places=6)
    max_cost_trx: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=6)


class RentalSettings(BaseModel):
    enabled: bool = False
    products: list[RentalProduct] = Field(default_factory=list)
    poll_seconds: float = Field(default=5, ge=1, le=300)
    lease_seconds: int = Field(default=180, ge=180, le=3600)
    batch_size: int = Field(default=20, ge=1, le=100)
    quote_retry_limit: int = Field(default=3, ge=1, le=100)
    max_submit_attempts: int = Field(default=5, ge=1, le=100)
    order_concurrency: int = Field(default=8, ge=1, le=64)
    notification_concurrency: int = Field(default=4, ge=1, le=32)
    idle_poll_seconds: float = Field(default=1, ge=0.05, le=30)
    quote_cache_seconds: float = Field(default=2, ge=0, le=10)
    balance_cache_seconds: float = Field(default=1, ge=0, le=5)
    upstream_concurrency: int = Field(default=4, ge=1, le=32)
    upstream_requests_per_second: float = Field(default=20, ge=1, le=1000)
    upstream_orders_per_second: float = Field(default=5, ge=1, le=1000)

    @field_validator("products")
    @classmethod
    def unique_products(cls, value: list[RentalProduct]) -> list[RentalProduct]:
        keys = [(p.energy_amount, p.duration_minutes) for p in value]
        if len(keys) != len(set(keys)):
            raise ValueError("产品的能量数量和租期不能重复")
        amounts = [p.energy_amount for p in value]
        if len(amounts) != len(set(amounts)):
            # /rent 只按能量数量匹配套餐(租期不给用户选),同能量多租期无法区分
            raise ValueError("同一能量数量只能上架一个租期")
        return value


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_LOGGING_")

    level: str = "INFO"  # DEBUG/INFO/WARNING/ERROR/CRITICAL
    log_dir: str = ""  # 日志文件目录;空表示只输出到 stdout
    json_logs: bool = False  # stdout 是否用 JSON 格式(生产环境建议开)

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in LOG_LEVELS:
            raise ValueError("应为 DEBUG/INFO/WARNING/ERROR/CRITICAL 之一")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENERGY_BOT_", env_nested_delimiter="__")

    bot_token: str = ""  # @BotFather 的 bot token;或设 ENERGY_BOT_BOT_TOKEN
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    payment: PaymentSettings = Field(default_factory=PaymentSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    rental: RentalSettings = Field(default_factory=RentalSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Pydantic 对不同来源的嵌套字典递归合并,环境变量仅覆盖指定字段。
        return env_settings, init_settings, dotenv_settings, file_secret_settings


def load_settings(path: Path | None = None) -> Settings:
    path = resolve_config_path(path)
    if not path.is_file():
        raise SystemExit(
            f"找不到配置文件 {path}。"
            "可将 config.example.yaml 复制到上述路径,"
            "或通过 --config 参数 / ENERGY_BOT_CONFIG 环境变量指定配置文件位置"
        )
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"配置文件 {path} 不是合法的 YAML:\n{exc}") from exc
    if (
        isinstance(data, dict)
        and isinstance(data.get("database"), dict)
        and "dsn" in data["database"]
    ):
        raise SystemExit(
            f"配置文件 {path} 不允许 database.dsn:"
            "连接串只能通过环境变量 ENERGY_BOT_DATABASE__DSN 提供,"
            "配置文件里请用 address/username/password/database"
        )
    try:
        return Settings(**(data if isinstance(data, dict) else {}))
    except ValidationError as exc:
        raise SystemExit(f"配置文件 {path} 无效:\n{exc}") from exc
