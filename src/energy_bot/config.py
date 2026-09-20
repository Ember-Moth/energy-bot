"""配置加载。

默认从平台用户配置目录读取 ``config.yaml``;``--config`` 参数或 ``ENERGY_BOT_CONFIG``
环境变量可指定其他路径。环境变量(前缀 ``ENERGY_BOT_``)优先级高于 YAML 文件,
嵌套键用双下划线(如 ``ENERGY_BOT_WEBHOOK__PORT``),方便注入密钥而不提交到代码库。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_NAME = "config.yaml"
CONFIG_ENV = "ENERGY_BOT_CONFIG"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


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
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("必须是 https:// 开头的公网地址,如 https://bot.example.com")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = value.strip() or "/webhook"
        return value if value.startswith("/") else f"/{value}"


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
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

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
    try:
        return Settings(**(data if isinstance(data, dict) else {}))
    except ValidationError as exc:
        raise SystemExit(f"配置文件 {path} 无效:\n{exc}") from exc
