from dataclasses import dataclass, field
from pathlib import Path

import yaml
from platformdirs import user_config_dir

DEFAULT_CONFIG_NAME = "config.yaml"

LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def default_config_path() -> Path:
    """按平台约定解析用户配置目录中的配置文件。

    macOS: ~/Library/Application Support/energy-bot/config.yaml
    Linux: ~/.config/energy-bot/config.yaml
    """
    return Path(user_config_dir("energy-bot")) / DEFAULT_CONFIG_NAME


@dataclass(frozen=True, slots=True)
class WebhookSettings:
    # 公网 HTTPS 基地址,如 https://example.com,更新会 POST 到 {base_url}{path}
    base_url: str
    host: str = "0.0.0.0"  # 本地监听地址
    port: int = 8080
    path: str = "/webhook"
    # Telegram 通过 X-Telegram-Bot-Api-Secret-Token 头携带,为空则每次启动自动生成
    secret_token: str = ""


@dataclass(frozen=True, slots=True)
class LogSettings:
    level: str = "INFO"  # DEBUG / INFO / WARNING / ERROR / CRITICAL
    file: str = ""  # 日志文件路径;留空仅输出到 stderr
    file_max_bytes: int = 10 * 1024 * 1024  # 单文件上限,超出后滚动
    file_backup_count: int = 5  # 滚动保留的历史文件数


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    webhook: WebhookSettings
    log: LogSettings = field(default_factory=LogSettings)


def load_settings(path: Path | None = None) -> Settings:
    path = path or default_config_path()
    if not path.is_file():
        raise SystemExit(
            f"找不到配置文件 {path}。"
            "可将 config.example.yaml 复制到上述路径,"
            "或通过 --config 参数指定配置文件位置"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"配置文件 {path} 不是合法的 YAML:\n{exc}") from exc

    token = data.get("bot_token", "")
    if not isinstance(token, str) or not token.strip():
        raise SystemExit(f"配置文件 {path} 缺少有效的 bot_token 字段")

    return Settings(
        bot_token=token.strip(),
        webhook=_parse_webhook(data.get("webhook"), path),
        log=_parse_log(data.get("log"), path),
    )


def _parse_webhook(raw: object, path: Path) -> WebhookSettings:
    raw_webhook = raw or {}
    if not isinstance(raw_webhook, dict):
        raise SystemExit(f"配置文件 {path} 的 webhook 段格式错误,应为键值映射")

    base_url = str(raw_webhook.get("base_url", "")).strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise SystemExit(f"配置文件 {path} 的 webhook.base_url 无效,应形如 https://example.com")

    hook_path = str(raw_webhook.get("path", "/webhook")).strip() or "/webhook"
    if not hook_path.startswith("/"):
        hook_path = f"/{hook_path}"

    port = raw_webhook.get("port", 8080)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise SystemExit(f"配置文件 {path} 的 webhook.port 无效,应为 1-65535 的整数")

    return WebhookSettings(
        base_url=base_url,
        host=str(raw_webhook.get("host", "0.0.0.0")),
        port=port,
        path=hook_path,
        secret_token=str(raw_webhook.get("secret_token", "")).strip(),
    )


def _parse_log(raw: object, path: Path) -> LogSettings:
    raw_log = raw or {}
    if not isinstance(raw_log, dict):
        raise SystemExit(f"配置文件 {path} 的 log 段格式错误,应为键值映射")

    level = str(raw_log.get("level", "INFO")).strip().upper()
    if level not in LOG_LEVELS:
        raise SystemExit(
            f"配置文件 {path} 的 log.level 无效,应为 {'/'.join(sorted(LOG_LEVELS))} 之一"
        )

    return LogSettings(
        level=level,
        file=str(raw_log.get("file", "")).strip(),
        file_max_bytes=raw_log.get("file_max_bytes", 10 * 1024 * 1024),
        file_backup_count=raw_log.get("file_backup_count", 5),
    )
