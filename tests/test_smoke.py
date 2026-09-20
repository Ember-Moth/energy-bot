from pathlib import Path

import pytest

from energy_bot.config import Settings, WebhookSettings, default_config_path, load_settings
from energy_bot.handlers import routers
from energy_bot.middlewares.logging import LoggingMiddleware


def test_routers_registered() -> None:
    names = {router.name for router in routers}
    assert {"start", "echo"} <= names


def test_settings_holds_token() -> None:
    settings = Settings(
        bot_token="abc",
        webhook=WebhookSettings(base_url="https://example.com"),
    )
    assert settings.bot_token == "abc"


async def test_logging_middleware_passes_event_through() -> None:
    seen: list[tuple[object, dict[str, int]]] = []

    async def handler(event: object, data: dict[str, int]) -> None:
        seen.append((event, data))

    await LoggingMiddleware()(handler, "event", {"k": 1})

    assert seen == [("event", {"k": 1})]


def test_load_settings_reads_webhook(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "bot_token: abc\n"
        "webhook:\n"
        "  base_url: https://example.com/\n"
        "  port: 9443\n"
        "  secret_token: s3cret\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.bot_token == "abc"
    assert settings.webhook.base_url == "https://example.com"  # 去掉尾部斜杠
    assert settings.webhook.port == 9443
    assert settings.webhook.secret_token == "s3cret"


def test_load_settings_webhook_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "bot_token: abc\nwebhook:\n  base_url: https://example.com\n",
        encoding="utf-8",
    )
    webhook = load_settings(config).webhook
    assert webhook.host == "0.0.0.0"
    assert webhook.port == 8080
    assert webhook.path == "/webhook"
    assert webhook.secret_token == ""


def test_default_config_path_in_user_config_dir() -> None:
    path = default_config_path()
    assert path.name == "config.yaml"
    assert "energy-bot" in path.parts


def test_missing_config_file_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_settings(tmp_path / "config.yaml")


def test_missing_base_url_exits(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("bot_token: abc\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_settings(config)


def test_missing_token_exits_cleanly(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'bot_token: ""\nwebhook:\n  base_url: https://example.com\n', encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        load_settings(config)
