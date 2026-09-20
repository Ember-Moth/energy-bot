from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


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
class Settings:
    bot_token: str
    webhook: WebhookSettings


def load_settings(path: Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise SystemExit(
            f"找不到配置文件 {path}:"
            "请复制 config.example.yaml 为 config.yaml,并填入从 @BotFather 获取的 token"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"配置文件 {path} 不是合法的 YAML:\n{exc}") from exc

    token = data.get("bot_token", "")
    if not isinstance(token, str) or not token.strip():
        raise SystemExit(f"配置文件 {path} 缺少有效的 bot_token 字段")

    raw_webhook = data.get("webhook") or {}
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

    return Settings(
        bot_token=token.strip(),
        webhook=WebhookSettings(
            base_url=base_url,
            host=str(raw_webhook.get("host", "0.0.0.0")),
            port=port,
            path=hook_path,
            secret_token=str(raw_webhook.get("secret_token", "")).strip(),
        ),
    )
